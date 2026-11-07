"""Сцена выставочного экрана из исходного проекта UE4 «Наливочная».

    python -m tools.ue4.expo_scene --content <Nalivochnaya/Content> --meshes <выгрузка umodel>

Расстановка берётся из карты уровня L_Example.umap, а не подбирается на глаз:
у каждого компонента свойства уровня ложатся поверх шаблона из BP-класса и
его родителей (SCS и InheritableComponentHandler). На выходе — layout.json:
иерархия узлов в осях three.js (метры, Y вверх), меши, материалы с цветами из
MI. glTF мешей берутся из выгрузки umodel по собранному паку HTML5 (в
редакторских ассетах готовых вершин нет) и копируются рядом.

Оси: glTF от umodel = (X, Z, Y) · 0,01 от UE, поэтому матрица UE M (строка-вектор)
переходит в three как C·Mᵀ·C⁻¹, где C меняет местами Y и Z.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import struct
import zlib
from pathlib import Path

from .uasset import Package

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "services" / "instructor_ui" / "static" / "expo" / "models"
MAP = "Maps/L_Example.umap"

#: Компоненты, у которых бывает трансформ.
SCENE_CLASSES = {
    "SceneComponent", "StaticMeshComponent", "SkeletalMeshComponent", "ArrowComponent", "BillboardComponent",
    "CableComponent", "SpringArmComponent", "CameraComponent", "CapsuleComponent", "ParticleSystemComponent",
    "DrawSphereComponent", "BoxComponent", "SphereComponent", "ChildActorComponent", "WidgetComponent",
}
KEYS = ("RelativeLocation", "RelativeRotation", "RelativeScale3D", "StaticMesh", "OverrideMaterials",
        "bVisible", "bHiddenInGame", "bAbsoluteLocation", "bAbsoluteRotation", "bAbsoluteScale")

#: Акторы, которых на экране нет: вода и эффекты растворения (материалы UE без
#: аналога), вспомогательные плоскости и объект, привязанный в уровне дважды.
SKIP_ACTORS = ("BP_DisolveBox", "WarningWater", "Plane", "BP_CisternWater", "BP_WaterCap", "BP_CisternWaterCap")
SKIP_ACTOR_LABELS = {"SM_NalivSUG"}

#: Материалы без MI в исходниках: то, что в паке осталось WorldGridMaterial, в
#: UE рисуется базовым M_Master (серый 0,40 линейный).
MASTER = {"color": [0.402, 0.402, 0.402], "metal": 0.0, "rough": 0.6}


def srgb(c: float) -> float:
    c = max(0.0, min(1.0, c))
    return 12.92 * c if c <= 0.0031308 else 1.055 * c ** (1 / 2.4) - 0.055


# --- материалы -------------------------------------------------------------

def read_materials(content: Path) -> dict:
    """Цвет/металл/шероховатость из MaterialInstanceConstant (параметры M_Master)."""
    out: dict[str, dict] = {}
    for f in sorted((content / "Materials").rglob("*.uasset")):
        p = Package(f)
        for e in p.exports:
            if p.export_class(e) != "MaterialInstanceConstant":
                continue
            pr = p.props(e)
            vec, sca = {}, {}
            for it in pr.get("VectorParameterValues", {}).get("items", []):
                vec[it.get("ParameterInfo", {}).get("Name")] = it.get("ParameterValue")
            for it in pr.get("ScalarParameterValues", {}).get("items", []):
                sca[it.get("ParameterInfo", {}).get("Name")] = it.get("ParameterValue")
            parent = p.ref(pr["Parent"]["ref"]) if "Parent" in pr else None
            color = vec.get("Color") or vec.get("Base Color") or MASTER["color"]
            m = {
                "color": "#%02x%02x%02x" % tuple(round(srgb(c) * 255) for c in color[:3]),
                "metal": round(sca.get("Metallic", 0.0), 3),
                "rough": round(sca.get("Roughness", 0.6), 3),
                "parent": parent,
            }
            if parent and parent.startswith("M_MasterRabota"):
                # Трубы с бегущими линиями потока: основа серая, линии — цвет продукта.
                m["color"] = "#%02x%02x%02x" % tuple(round(srgb(c) * 255) for c in MASTER["color"])
                line = vec.get("ColorLine")
                if line:
                    m["flow"] = "#%02x%02x%02x" % tuple(round(srgb(c) * 255) for c in line[:3])
            if parent and parent.startswith(("M_Water", "M_Faded")):
                m["skip"] = True
            out[f.stem] = m
    out["M_MasterRabota_Red_1"] = {**out.get("M_MasterRabota_Red_2", {}), "color": "#aaaaaa"}
    out["WorldGridMaterial"] = {"color": "#aaaaaa", "metal": 0.0, "rough": 0.6}
    out["M_SIBUR"] = {"logo": True}
    return out


# --- уровень ---------------------------------------------------------------

class Level:
    def __init__(self, content: Path):
        self.content = content
        self._pk: dict[str, Package | None] = {}
        self.m = Package(content / MAP)

    def pkg(self, game_path: str) -> Package | None:
        if game_path not in self._pk:
            f = self.content / (game_path[len("/Game/"):] + ".uasset")
            self._pk[game_path] = Package(f) if f.exists() else None
        return self._pk[game_path]

    def class_chain(self, game_path: str, cls_name: str) -> list[Package]:
        """BP-класс и его BP-родители, от дочернего к базовому."""
        out = []
        while game_path and game_path.startswith("/Game/"):
            p = self.pkg(game_path)
            if not p:
                break
            ci = next((i for i, e in enumerate(p.exports, 1) if e["name"] == cls_name), None)
            if not ci:
                break
            out.append(p)
            sup = p.exports[ci - 1]["super"]
            if sup >= 0:
                break
            game_path, cls_name = p.path_of(sup).rsplit(".", 1)
        return out

    @staticmethod
    def _take(p: Package, pr: dict, into: dict) -> None:
        for k in KEYS:
            if k not in pr:
                continue
            v = pr[k]
            if k == "StaticMesh":
                v = p.path_of(v["ref"]) if v["ref"] else None
            elif k == "OverrideMaterials":
                v = [p.path_of(x["ref"]).rsplit(".", 1)[-1] if x["ref"] else None for x in v]
            into[k] = v

    def components(self) -> dict[int, dict]:
        m = self.m
        level = next(i for i, e in enumerate(m.exports, 1) if e["name"] == "PersistentLevel")
        comps: dict[int, dict] = {}
        for ai, a in enumerate(m.exports, 1):
            if a["outer"] != level:
                continue
            cls_idx = a["class"]
            cls_name = m.ref(cls_idx)
            chain = []
            if cls_idx < 0:
                chain = self.class_chain(m.path_of(m.imports[-cls_idx - 1]["outer"]), cls_name)
            apr = m.props(a)
            label = apr.get("ActorLabel") or a["name"]
            for ci, c in enumerate(m.exports, 1):
                if c["outer"] != ai or m.export_class(c) not in SCENE_CLASSES:
                    continue
                d: dict = {}
                for p in reversed(chain):
                    for e in p.exports:
                        if e["name"] == f"{c['name']}_GEN_VARIABLE":
                            self._take(p, p.props(e), d)
                pr = m.props(c)
                self._take(m, pr, d)
                d.update(
                    idx=ci, name=c["name"], cls=m.export_class(c), actor=label, actor_class=cls_name,
                    folder=apr.get("FolderPath"), tags=apr.get("Tags") or [], actor_hidden=bool(apr.get("bHidden")),
                    parent=pr["AttachParent"]["ref"] if "AttachParent" in pr else None,
                )
                comps[ci] = d
        return comps


def rot_matrix(pyr):
    P, Y, R = (math.radians(a) for a in pyr)
    SP, CP, SY, CY, SR, CR = math.sin(P), math.cos(P), math.sin(Y), math.cos(Y), math.sin(R), math.cos(R)
    return [[CP * CY, CP * SY, SP],
            [SR * SP * CY - CR * SY, SR * SP * SY + CR * CY, -SR * CP],
            [-(CR * SP * CY + SR * SY), CY * SR - CR * SP * SY, CR * CP]]


def to_three(loc, rot, scl) -> list[float]:
    """Локальный трансформ UE -> Matrix4 three (по столбцам)."""
    r = rot_matrix(rot)
    rows = [[r[i][j] * scl[i] for j in range(3)] for i in range(3)]   # строки: оси X, Y, Z
    A = [[rows[j][i] for j in range(3)] for i in range(3)]             # A = Mᵀ (столбец-вектор)
    sw = (0, 2, 1)
    B = [[A[sw[i]][sw[j]] for j in range(3)] for i in range(3)]        # C·A·C⁻¹
    t = [loc[0] * 0.01, loc[2] * 0.01, loc[1] * 0.01]
    return [round(v, 6) for v in (B[0][0], B[1][0], B[2][0], 0, B[0][1], B[1][1], B[2][1], 0,
                                  B[0][2], B[1][2], B[2][2], 0, t[0], t[1], t[2], 1)]


def mesh_file(path: str) -> str:
    """/Game/Import/Naliv/V2/Cable.Cable -> Game/Import/Naliv/V2/Cable.gltf; кубы движка — примитивом."""
    pkg = path.rsplit(".", 1)[0]
    if pkg.startswith("/Engine/BasicShapes/"):
        return "engine:" + pkg.rsplit("/", 1)[1]
    return pkg.lstrip("/") + ".gltf"


# --- текстура логотипа -----------------------------------------------------

def read_tga(path: Path) -> tuple[int, int, list[tuple[int, int, int]]]:
    b = path.read_bytes()
    idlen, cmap, kind = b[0], b[1], b[2]
    w, h, bpp, desc = struct.unpack_from("<HHBB", b, 12)
    assert cmap == 0 and kind in (2, 10) and bpp in (24, 32), (cmap, kind, bpp)
    n, p, px = bpp // 8, 18 + idlen, []
    while len(px) < w * h:
        if kind == 2:
            px.append(tuple(b[p:p + n])); p += n
            continue
        head = b[p]; p += 1
        cnt = (head & 0x7F) + 1
        if head & 0x80:
            px.extend([tuple(b[p:p + n])] * cnt); p += n
        else:
            px.extend(tuple(b[p + i * n:p + i * n + n]) for i in range(cnt)); p += n * cnt
    rows = [px[y * w:(y + 1) * w] for y in range(h)]
    if not desc & 0x20:            # начало строк снизу
        rows.reverse()
    return w, h, [(q[2], q[1], q[0]) for row in rows for q in row]


def write_png_rgba(path: Path, w: int, h: int, rgba: bytes) -> None:
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
                     + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def logo_png(tga: Path, out: Path) -> None:
    """M_SIBUR — маскированный материал: чёрное отсекается (порог 0,333)."""
    w, h, px = read_tga(tga)
    buf = bytearray()
    for r, g, b in px:
        buf += bytes((r, g, b, 255 if max(r, g, b) > 40 else 0))
    write_png_rgba(out, w, h, bytes(buf))


# --- сборка ----------------------------------------------------------------

def build(content: Path, meshes: Path, out: Path) -> dict:
    lv = Level(content)
    comps = lv.components()
    mats = read_materials(content)

    def skipped(d: dict) -> bool:
        return d["actor"] in SKIP_ACTOR_LABELS or d["actor_class"].startswith(SKIP_ACTORS) \
            or d["actor"].startswith(SKIP_ACTORS)

    # Узлы, нужные мешам: сам компонент с мешем и все его предки.
    need: set[int] = set()
    for ci, d in comps.items():
        if d["cls"] == "StaticMeshComponent" and d.get("StaticMesh") and not skipped(d):
            if d.get("bVisible") is False or d.get("bHiddenInGame") or d["actor_hidden"]:
                continue
            if any(m and mats.get(m, {}).get("skip") for m in d.get("OverrideMaterials") or []):
                continue
            while ci and ci in comps and ci not in need:
                need.add(ci)
                ci = comps[ci]["parent"]

    nodes, mesh_list, files = [], [], set()
    for ci in sorted(need):
        d = comps[ci]
        nodes.append({
            "id": ci, "parent": d["parent"] if d["parent"] in need else None,
            "actor": d["actor"], "cls": d["actor_class"], "comp": d["name"],
            **({"tags": d["tags"]} if d["tags"] else {}),
            "m": to_three(d.get("RelativeLocation", [0, 0, 0]), d.get("RelativeRotation", [0, 0, 0]),
                          d.get("RelativeScale3D", [1, 1, 1])),
        })
        if d["cls"] == "StaticMeshComponent" and d.get("StaticMesh"):
            f = mesh_file(d["StaticMesh"])
            mesh_list.append({"node": ci, "mesh": f, **({"mats": d["OverrideMaterials"]} if d.get("OverrideMaterials") else {})})
            if not f.startswith("engine:"):
                files.add(f)

    # glTF и их .bin рядом со страницей
    for f in sorted(files):
        src = meshes / f
        if not src.exists():
            raise SystemExit(f"нет меша в выгрузке umodel: {src}")
        dst = out / f
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        for buf in json.loads(src.read_text(encoding="utf-8")).get("buffers", []):
            shutil.copyfile(src.parent / buf["uri"], dst.parent / buf["uri"])
    logo_png(meshes / "Game" / "Materials" / "T_SIBUR.tga", out / "sibur_strip.png")

    used = {m for x in mesh_list for m in x.get("mats", []) if m}
    layout = {
        "source": "L_Example.umap",
        "nodes": nodes,
        "meshes": mesh_list,
        "materials": {k: v for k, v in mats.items() if k in used or k in ("WorldGridMaterial", "M_SIBUR")},
    }
    (out / "layout.json").write_text(json.dumps(layout, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    return layout


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--content", type=Path, required=True, help="папка Content исходного проекта")
    ap.add_argument("--meshes", type=Path, required=True, help="выгрузка umodel -export -gltf (папка с Game/)")
    ap.add_argument("--out", type=Path, default=OUT)
    a = ap.parse_args()
    layout = build(a.content, a.meshes, a.out)
    print(f"узлов {len(layout['nodes'])}, мешей {len(layout['meshes'])}, материалов {len(layout['materials'])} -> {a.out}")


if __name__ == "__main__":
    main()
