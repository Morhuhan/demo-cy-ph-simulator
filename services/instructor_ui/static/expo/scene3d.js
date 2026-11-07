// 3D-сцена эстакады из исходного проекта заказчика «Наливочная» (UE4).
// Расстановка — из карты уровня L_Example.umap (models/layout.json, собирает
// tools/ue4/expo_scene.py), меши — из сборки. Здесь только то, чего в
// уровне нет: заезд цистерны под стояк, работа стояка С-14ЖД-К на его
// шарнирах, кабель заземления, камера от первого лица.
import * as THREE from 'three';
import { GLTFLoader } from 'three/addons/loaders/GLTFLoader.js';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { OutlinePass } from 'three/addons/postprocessing/OutlinePass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';

const EYE = 1.65;              // глаза оператора над полом
const ARM_DROP_GAP = 0.02;     // крышка наконечника садится на горловину с этим зазором

export async function createScene(container, base = '') {
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.0;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  container.appendChild(renderer.domElement);

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x0b0f12);
  scene.fog = new THREE.Fog(0x0b0f12, 60, 140);

  const camera = new THREE.PerspectiveCamera(40, 16 / 9, 0.05, 400);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.06;
  controls.maxPolarAngle = Math.PI * 0.49;
  controls.minDistance = 2; controls.maxDistance = 70;

  // Свет как в уровне: направленный под 40° вдоль +X и небесный.
  scene.add(new THREE.HemisphereLight(0xe6eef8, 0x3a3f44, 1.5));
  const sun = new THREE.DirectionalLight(0xfff6e8, 2.6);
  sun.position.set(-14, 12.5, 6);
  sun.target.position.set(0, 0, 0);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  Object.assign(sun.shadow.camera, { left: -22, right: 22, top: 22, bottom: -22, near: 1, far: 70 });
  sun.shadow.bias = -0.0004;
  scene.add(sun, sun.target);

  const ground = new THREE.Mesh(new THREE.PlaneGeometry(400, 400),
    new THREE.MeshStandardMaterial({ color: 0x2b3034, roughness: 1 }));
  ground.rotation.x = -Math.PI / 2; ground.position.y = 0.0; ground.receiveShadow = true;
  ground.userData.floor = true;
  scene.add(ground);

  // --- уровень --------------------------------------------------------------
  const layout = await (await fetch(`${base}layout.json`)).json();
  const nodes = new Map();       // id компонента -> Object3D
  const actors = new Map();      // подпись актора -> корневой узел
  for (const n of layout.nodes) {
    const o = new THREE.Group();
    new THREE.Matrix4().fromArray(n.m).decompose(o.position, o.quaternion, o.scale);
    o.userData = { actor: n.actor, cls: n.cls, comp: n.comp, tags: n.tags || [] };
    nodes.set(n.id, o);
  }
  for (const n of layout.nodes) {
    const o = nodes.get(n.id);
    const parent = n.parent ? nodes.get(n.parent) : scene;
    parent.add(o);
    if (!n.parent || nodes.get(n.parent).userData.actor !== n.actor) {
      if (!actors.has(n.actor)) actors.set(n.actor, o);
    }
  }

  const mats = {};
  for (const [name, m] of Object.entries(layout.materials)) {
    if (m.logo) continue;
    // M_Master двусторонний: у части мешей нет обратных граней
    mats[name] = new THREE.MeshStandardMaterial({ name, color: m.color, metalness: m.metal ?? 0,
      roughness: m.rough ?? 0.6, side: THREE.DoubleSide });
  }
  const logo = await new THREE.TextureLoader().loadAsync(`${base}sibur_strip.png`);
  logo.flipY = false; logo.colorSpace = THREE.SRGBColorSpace; logo.anisotropy = 8;
  mats.M_SIBUR = new THREE.MeshStandardMaterial({ name: 'M_SIBUR', map: logo, alphaTest: 0.5, roughness: 0.35 });
  const fallback = mats.WorldGridMaterial;

  const loader = new GLTFLoader();
  const gltfs = new Map();
  const loadGltf = (file) => {
    if (!gltfs.has(file)) {
      gltfs.set(file, loader.loadAsync(base + file).then((g) => {
        // номер слота материала меша UE = индекс материала glTF
        g.scene.traverse((o) => { if (o.isMesh) o.userData.slot = g.parser.associations.get(o.material)?.materials ?? 0; });
        return g.scene;
      }));
    }
    return gltfs.get(file);
  };
  const engineGeo = { 'engine:Cube': new THREE.BoxGeometry(1, 1, 1), 'engine:Plane': new THREE.PlaneGeometry(1, 1).rotateX(-Math.PI / 2) };

  const allMeshes = [];
  await Promise.all(layout.meshes.map(async (m) => {
    const node = nodes.get(m.node);
    let obj;
    if (m.mesh.startsWith('engine:')) {
      obj = new THREE.Mesh(engineGeo[m.mesh]);
      obj.userData.slot = 0;
    } else {
      obj = (await loadGltf(m.mesh)).clone(true);
    }
    obj.traverse((o) => {
      if (!o.isMesh) return;
      const own = o.material && o.material.name;
      const over = m.mats && m.mats[o.userData.slot];
      o.material = mats[over] || mats[own] || fallback;
      o.castShadow = o.receiveShadow = true;
      o.userData.actor = node.userData.actor;
      allMeshes.push(o);
    });
    node.add(obj);
  }));

  const actor = (label) => actors.get(label);
  const meshesOf = (...labels) => allMeshes.filter((o) => labels.includes(o.userData.actor));
  const worldOf = (o, local = [0, 0, 0]) => { o.updateWorldMatrix(true, false); return o.localToWorld(new THREE.Vector3(...local)); };
  // Перенести узлы под новую группу, сохранив их положение в мире.
  function pivot(parent, world, ...children) {
    scene.updateMatrixWorld(true);
    const g = new THREE.Group();
    parent.add(g);
    g.position.copy(parent.worldToLocal(world.clone()));
    g.updateMatrixWorld(true);
    children.forEach((c) => g.attach(c));
    return g;
  }
  // Направление из мира в оси родителя группы (с его масштабом).
  function localDir(group, worldVec, unit = true) {
    scene.updateMatrixWorld(true);
    const p = group.parent;
    const o = p.worldToLocal(new THREE.Vector3());
    const v = p.worldToLocal(worldVec.clone()).sub(o);
    return unit ? v.normalize() : v;
  }
  scene.updateMatrixWorld(true);

  // --- цистерна ---------------------------------------------------------------
  const tank = actor('BP_Cistern');
  const tankMeshes = meshesOf('BP_Cistern');
  const tankMats = [...new Set(tankMeshes.map((o) => o.material))].map((m) => m.clone());
  tankMeshes.forEach((o) => { o.material = tankMats.find((m) => m.name === o.material.name) || o.material; });
  const tankStartZ = tank.position.z;

  // Крышка люка: ушко шарнира на локальном x ≈ −0,13, диск — вокруг x ≈ 0,33.
  const cupNode = actor('BP_CisternCup');
  const cupMesh = cupNode.children.find((c) => c.userData.comp === 'MainMesh');
  const hingeW = worldOf(cupMesh, [-0.13, 0.06, 0]);
  const hingeAxisW = worldOf(cupMesh, [-0.13, 0.06, 1]).sub(hingeW).normalize();
  const hinge = pivot(cupNode.parent, hingeW, cupNode);
  const hingeAxis = localDir(hinge, hingeAxisW);
  const neckLocal = cupMesh.worldToLocal(worldOf(cupMesh, [0.33, 0, 0]));   // центр горловины в осях крышки

  // --- стояк С-14ЖД-К ---------------------------------------------------------
  // Плечо T_03 сложено к колонне. Рабочее положение: поворот вокруг шарнира
  // Sharnir_1 на 90° к цистерне и наклон вниз; наконечник висит отвесно.
  const sh1 = actor('BP_Naliv_Sharnir_1');
  const sh2 = actor('BP_Naliv_Sharnir_2');
  const t03 = actor('BP_Naliv_T_03');
  const t06 = actor('BP_Naliv_T_06');
  const sh1W = worldOf(sh1);
  const sh2W = worldOf(sh2);
  const armDir = sh2W.clone().sub(sh1W).setY(0).normalize();
  const tiltAxis = new THREE.Vector3(0, 1, 0).cross(armDir).normalize();
  const armLen = sh2W.clone().sub(sh1W).setY(0).length();

  const UP = new THREE.Vector3(0, 1, 0);
  const yaw = pivot(sh1.parent, sh1W, t03);             // поворот плеча в плане
  const tilt = pivot(yaw, sh1W, t03);                    // наклон плеча
  const hang = pivot(sh2.parent, sh2W, sh2);             // наконечник остаётся отвесным
  const slide = pivot(t06.parent, worldOf(t06), t06);    // выдвижная внутренняя труба
  // Оси — в осях родителя каждой группы: узлы UE повёрнуты и масштабированы.
  const yawAxis = localDir(yaw, UP);
  const tiltAxisL = localDir(tilt, tiltAxis);
  const hangAxis = localDir(hang, tiltAxis);
  const slideDown = localDir(slide, UP.clone().negate(), false);   // 1 м вниз
  const slideBase = slide.position.clone();

  function setArm(swing, angle) {
    yaw.quaternion.setFromAxisAngle(yawAxis, swing);
    tilt.quaternion.setFromAxisAngle(tiltAxisL, angle);
    hang.quaternion.setFromAxisAngle(hangAxis, -angle);
  }

  // Крышка наконечника (без датчика уровня, он висит ниже) и труба в мире
  const boxOf = (meshes) => meshes.reduce((b, m) => b.expandByObject(m), new THREE.Box3());
  const nalCup = meshesOf('BP_Naliv_Cup');
  const pipeBox = () => new THREE.Box3().setFromObject(t06);

  // Где встанет цистерна: горловина под трубой в рабочем положении стояка.
  // Наклон — такой, чтобы крышка наконечника села на горловину; поворот —
  // такой, чтобы труба пришла на ось пути (по ней цистерну и подгоняют).
  const neckW0 = cupMesh.localToWorld(neckLocal.clone());
  const neckTopY = boxOf(meshesOf('BP_CisternCup')).max.y - 0.04;
  const drop0 = boxOf(nalCup).min.y - (neckTopY + ARM_DROP_GAP);
  const TILT = Math.asin(Math.min(0.9, drop0 / armLen));
  // Подъём перед поворотом: низ трубы — на 15 см выше всего на цистерне (с поручнями площадки).
  const LIFT = -Math.asin(Math.min(0.9, Math.max(0, boxOf(tankMeshes).max.y + 0.15 - pipeBox().min.y) / armLen));
  const pipeAtSwing = (s) => { setArm(s, TILT); scene.updateMatrixWorld(true); return pipeBox().getCenter(new THREE.Vector3()); };
  let lo = Math.PI / 4, hi = 3 * Math.PI / 4;
  const sideLo = Math.sign(pipeAtSwing(lo).x - neckW0.x);
  for (let i = 0; i < 30; i++) {
    const mid = (lo + hi) / 2;
    if (Math.sign(pipeAtSwing(mid).x - neckW0.x) === sideLo) lo = mid; else hi = mid;
  }
  const SWING = (lo + hi) / 2;
  const pipeAt = pipeAtSwing(SWING);
  const seatY = boxOf(nalCup).min.y;
  // Опускаясь, плечо проходит горизонталь, и труба уходит по дуге от стояка
  // (до 16 см). Горловину ставим ближе к середине этой дуги: в ней труба
  // идёт, не задевая кромку, а крышка наконечника всё равно её закрывает.
  setArm(SWING, 0); scene.updateMatrixWorld(true);
  const pipeFlat = pipeBox().getCenter(new THREE.Vector3());
  setArm(0, 0); scene.updateMatrixWorld(true);
  const tankParkZ = tankStartZ + (pipeAt.z + (pipeFlat.z - pipeAt.z) * 0.5 - neckW0.z);
  const pipeOffX = pipeAt.x - neckW0.x;

  // --- тормозные башмаки -------------------------------------------------------
  // В уровне все стоят в нуле, на место их ставит логика сборки. Здесь —
  // по башмаку перед крайними колёсами цистерны на рельс со стороны эстакады.
  // (подписи у башмаков в уровне повторяются, поэтому — по классу, а не по подписи)
  const stoppers = [...nodes.values()].filter((o) => o.userData.cls === 'BP_Interactive_Stopper_C'
    && o.userData.comp === 'DefaultSceneRoot');
  stoppers.forEach((s) => { s.visible = false; });
  const RAIL_X = -2.69, RAIL_Y = 0.92;
  const shoeAt = [[-5.75, 0], [6.0, Math.PI]];     // локальные x цистерны (до масштаба 0,75) и разворот
  const shoes = stoppers.slice(0, 2);
  function placeShoes(tz) {
    shoes.forEach((s, i) => {
      s.visible = true;
      s.position.set(RAIL_X, RAIL_Y, tz + shoeAt[i][0] * 0.75);
      s.rotation.set(0, shoeAt[i][1], 0);
    });
  }

  // --- заземление ----------------------------------------------------------------
  const zaz = actor('SM_Zazemlenie_001');
  const clip = actor('SM_Zazemlenie_002');
  const zazMeshes = [...meshesOf('SM_Zazemlenie_001'), ...meshesOf('SM_Zazemlenie_002')];
  const lampMesh = meshesOf('SM_Zazemlenie_001').find((o) => o.userData.slot === 3);
  const lampOff = lampMesh.material;
  const lampOn = new THREE.MeshStandardMaterial({ color: 0x2bff6a, emissive: 0x22ff66, emissiveIntensity: 2.4, roughness: 0.3 });
  const lampW = new THREE.Box3().setFromObject(lampMesh).getCenter(new THREE.Vector3());
  const clipW = new THREE.Box3().setFromObject(clip).getCenter(new THREE.Vector3());
  const lampGlow = new THREE.PointLight(0x2bff6a, 0, 1.2);
  lampGlow.position.copy(lampW).add(new THREE.Vector3(-0.15, 0, 0));
  scene.add(lampGlow);
  const zazW = worldOf(zaz);

  let cableMesh = null;
  const cableMat = new THREE.MeshStandardMaterial({ color: 0x1a1a1a, roughness: 0.8 });
  function buildCable(tz) {
    if (cableMesh) { scene.remove(cableMesh); cableMesh.geometry.dispose(); }
    // к раме цистерны со стороны эстакады
    const end = new THREE.Vector3(-2.32, 1.32, Math.min(clipW.z, tz + 3.2));
    const a = clipW.clone();
    const curve = new THREE.CatmullRomCurve3([
      a, a.clone().add(new THREE.Vector3(-0.12, -0.35, 0)),
      new THREE.Vector3((a.x + end.x) / 2, 0.75, (a.z + end.z) / 2),
      end.clone().add(new THREE.Vector3(0.05, -0.25, 0)), end,
    ]);
    cableMesh = new THREE.Mesh(new THREE.TubeGeometry(curve, 48, 0.012, 6), cableMat);
    cableMesh.castShadow = true;
    cableMesh.visible = false;
    scene.add(cableMesh);
  }

  // --- подсветка ------------------------------------------------------------------
  const composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const outline = new OutlinePass(new THREE.Vector2(1280, 720), scene, camera);
  Object.assign(outline, { edgeStrength: 6, edgeGlow: 0.9, edgeThickness: 2.5, pulsePeriod: 1.6 });
  outline.visibleEdgeColor.set(0x19e3d6); outline.hiddenEdgeColor.set(0x0b6f69);
  composer.addPass(outline);
  composer.addPass(new OutputPass());

  // --- узлы шагов и ракурсы -------------------------------------------------------
  const armMeshes = meshesOf('BP_Naliv_T_03', 'BP_Naliv_Sharnir_1', 'BP_Naliv_StoykaKompensatora_02', 'BP_Naliv_Compensator_01');
  const pipeMeshes = meshesOf('BP_Naliv_T_04', 'BP_Naliv_T_05', 'BP_Naliv_T_06', 'BP_Naliv_Cup', 'BP_Naliv_Prokladka', 'BP_Naliv_Datchik', 'BP_Naliv_Sharnir_2');
  const clampMeshes = meshesOf('BP_Naliv_Compensator_01', 'BP_Naliv_Compensator_02', 'BP_Naliv_StoykaKompensatora', 'BP_Naliv_StoykaKompensatora_02');
  const driveMeshes = meshesOf('BP_Naliv_T_04', 'BP_Naliv_T_05', 'BP_Naliv_T_06');
  const deckY = worldOf(actor('BP_Interactive_Perehod_7')).y - 0.06;
  const P = {
    zaz: zazW, lamp: lampW,
    hatch: () => cupMesh.localToWorld(neckLocal.clone()),
    sh1: sh1W, sh2: sh2W, col: worldOf(actor('BP_Naliv_Stoyka_01')),
  };
  // Ракурсы: [камера, точка взгляда] со стороны и стоя на месте оператора.
  const V = () => {
    const h = P.hatch();
    const extra = Object.fromEntries(Object.entries(tourViews).map(([k, f]) => [k, f()]));
    return {
      ...extra,
      overview: { orbit: [[-17, 10, 13], [-2, 4.2, 0]], eye: [[0.2, deckY + EYE, 6.5], [-3.4, 4.2, 0]] },
      // цистерна подходит по пути со стороны z < 0
      arrival: { orbit: [[-13.5, 7.5, 8], [-3.4, 2.4, -6]], eye: [[-0.6, deckY + EYE, 3.5], [-3.4, 2.6, -10]] },
      // Заземление стоит в проходе между цистерной и эстакадой — смотрим вдоль него.
      grounding: { orbit: [[-1.9, 4.7, P.zaz.z + 4.6], [P.zaz.x, 1.1, P.zaz.z]], eye: [[-1.98, 0.56 + EYE, P.zaz.z + 0.75], [P.lamp.x, P.lamp.y - 0.1, P.lamp.z]] },
      lamp: { orbit: [[-1.95, 2.6, P.lamp.z + 2.4], [P.lamp.x, P.lamp.y, P.lamp.z]], eye: [[-1.98, 0.56 + EYE, P.lamp.z + 0.75], [P.lamp.x, P.lamp.y, P.lamp.z]] },
      hatch: { orbit: [[-8.2, 8.6, h.z - 4.6], [h.x, h.y + 0.2, h.z]], eye: [[-0.95, deckY + EYE, h.z - 0.9], [h.x, h.y, h.z]] },
      pipe: { orbit: [[-9.5, 7.6, h.z + 5.2], [h.x + 0.4, h.y + 0.9, h.z]], eye: [[-0.95, deckY + EYE, h.z + 0.6], [h.x, h.y + 0.6, h.z]] },
      boom: { orbit: [[-8.8, 9.2, P.sh1.z + 6], [(P.sh1.x + P.col.x) / 2, P.sh1.y - 0.3, P.sh1.z - 0.6]], eye: [[-0.6, deckY + EYE, P.sh1.z + 1.4], [P.sh1.x, P.sh1.y - 0.4, P.sh1.z - 0.8]] },
      clamp: { orbit: [[-7.4, 8.4, P.sh1.z + 4.2], [P.sh1.x + 0.5, P.sh1.y - 0.2, P.sh1.z - 0.6]], eye: [[-0.8, deckY + EYE, P.sh1.z + 0.9], [P.sh1.x + 0.4, P.sh1.y - 0.3, P.sh1.z - 0.8]] },
      winch: { orbit: [[-7.8, 6.8, h.z + 3.4], [h.x, h.y + 0.8, h.z]], eye: [[-0.95, deckY + EYE, h.z + 0.6], [h.x, h.y + 0.8, h.z]] },
      report: { orbit: [[-17, 10, 13], [-2, 4.2, 0]], eye: [[0.2, deckY + EYE, 6.5], [-3.4, 4.2, 0]] },
    };
  };
  const parts = {
    grounding: { meshes: zazMeshes }, lamp: { meshes: [lampMesh] }, hatch: { meshes: meshesOf('BP_CisternCup') },
    pipe: { meshes: pipeMeshes }, boom: { meshes: armMeshes }, clamp: { meshes: clampMeshes },
    winch: { meshes: driveMeshes }, report: { meshes: [] }, overview: { meshes: [] },
  };

  // Пол под ногами: настил, мостики, путь, земля. Шаг на перепад больше 0,45 м
  // не делается — с настила не падают и на него не запрыгивают.
  const floorMeshes = [ground, ...meshesOf(...[...actors.keys()].filter((k) =>
    /Platforma|Perehod|Naliv_Rels|^Cube\d/.test(k)))];
  const ray = new THREE.Raycaster();
  function floorAt(x, z, fromY) {
    ray.set(new THREE.Vector3(x, fromY + 0.5, z), new THREE.Vector3(0, -1, 0));
    ray.far = 3;
    const hit = ray.intersectObjects(floorMeshes, false)[0];
    return hit ? hit.point.y : null;
  }
  // Узлы обхода эстакады («Структура объекта» в сборке заказчика). Ракурс —
  // по габаритам узла: со стороны пути сверху и с настила эстакады.
  const labels = [...actors.keys()];
  const like = (re) => meshesOf(...labels.filter((k) => re.test(k)));
  const deckZ = (z) => Math.max(-13.2, Math.min(14.8, z));
  function autoView(meshes, { near } = {}) {
    const b = boxOf(meshes), c = b.getCenter(new THREE.Vector3()), s = b.getSize(new THREE.Vector3());
    const r = near ? Math.max(1.6, s.length() * 0.9) : Math.min(16, Math.max(3, Math.max(s.x, s.y) * 1.3 + 2.5));
    const from = c.clone().add(new THREE.Vector3(-0.75, 0.55, 0.62).normalize().multiplyScalar(r));
    // Оператор смотрит с настила; на то, что у земли, — с земли: с настила
    // путь и башмаки закрывает котёл цистерны.
    let st;
    if (c.x < -4.6) st = [c.x - 1.3, 0, c.z + 1.4];            // за первым путём (сливные устройства)
    else if (c.y < 2.4) st = [-1.95, 0, c.z + 1.8];            // проход между путём и эстакадой
    if (st) st[1] = floorAt(st[0], st[2], 1.6) ?? 0.56;
    else st = [-0.6, deckY, deckZ(c.z + 2.4)];
    return { orbit: [from.toArray(), c.toArray()], eye: [[st[0], st[1] + EYE, st[2]], c.toArray()] };
  }
  const tour = {
    rails: { meshes: meshesOf('Naliv_Rels'), view: { orbit: [[-9, 4.2, 7.5], [-3.4, 0.8, 0]], eye: [[-0.6, deckY + EYE, 3], [-3.4, 0.8, -2]] } },
    shoes: { meshes: () => allMeshes.filter((o) => { let p = o; while (p && !shoes.includes(p)) p = p.parent; return !!p; }), near: true, dynamic: true },
    platform: { meshes: meshesOf('BP_Interactive_Platforma'), view: { orbit: [[-10, 10, 14], [0, 3.4, 2]], eye: [[0, deckY + EYE, 8], [0, deckY + 1, -6]] } },
    bridge: { meshes: meshesOf('BP_Interactive_Perehod_2', 'BP_Interactive_Perehod_6', 'BP_Interactive_Perehod_7', 'BP_Interactive_Perehod_8') },
    riser: { meshes: like(/^BP_Naliv[-_]|^BP_Nakonechik$|^Cylinder$|^SM_Naliv_All_Tube_Section_2$/) },
    sensor: { meshes: meshesOf('BP_Naliv_Datchik'), near: true },
    drain: { meshes: like(/^BP_Sliv_|^SM_Sliv_All_Zadvizhka$/) },
    pipeline: { meshes: like(/^Naliv_Truboprovod/) },
    lpg: { meshes: like(/^BP_SUG_|^SM_SUG_|^BP_Interactive_Naliv6$|^BP_NalivSUG/) },
    shunt: { meshes: meshesOf('BP_Interactive_Winch', 'BP_Interactive_Winch2', 'Cable2', 'Cable3', 'Cube2', 'Cube4') },
  };
  const tourViews = {};
  for (const [name, t] of Object.entries(tour)) {
    const meshes = typeof t.meshes === 'function' ? t.meshes : () => t.meshes;
    parts[name] = { get meshes() { return meshes(); } };
    if (t.view) tourViews[name] = () => t.view;
    else if (t.dynamic) tourViews[name] = () => autoView(meshes(), t);
    else { const v = autoView(meshes(), t); tourViews[name] = () => v; }
  }

  const tints = new Map();   // меш -> исходный материал, пока подсвечен
  function setTint(meshes, color) {
    for (const [m, orig] of tints) m.material = orig;
    tints.clear();
    for (const m of meshes) {
      tints.set(m, m.material);
      const t = m.material.clone();
      t.emissive = new THREE.Color(color); t.emissiveIntensity = 0;
      m.material = t;
    }
  }

  // --- камера: со стороны (orbit) и от первого лица (eye) ---------------------
  let mode = 'orbit';
  let current = 'overview';
  let fly = null;
  const look = { yaw: 0, pitch: 0 };           // взгляд от первого лица
  const eyePos = new THREE.Vector3();
  const ease = (t) => (t < 0.5 ? 4 * t * t * t : 1 - Math.pow(-2 * t + 2, 3) / 2);
  const lookFrom = (from, to) => {
    const d = to.clone().sub(from);
    return { yaw: Math.atan2(-d.x, -d.z), pitch: Math.atan2(d.y, Math.hypot(d.x, d.z)) };
  };
  const shortest = (a, b) => a + Math.atan2(Math.sin(b - a), Math.cos(b - a)) - a;

  function view(name, ms = 1800) {
    current = name;
    const v = V()[name] || V().overview;
    if (mode === 'orbit') {
      fly = { t0: performance.now(), ms, kind: 'orbit',
        p0: camera.position.clone(), p1: new THREE.Vector3(...v.orbit[0]),
        q0: controls.target.clone(), q1: new THREE.Vector3(...v.orbit[1]) };
    } else {
      const p1 = new THREE.Vector3(...v.eye[0]);
      const l1 = lookFrom(p1, new THREE.Vector3(...v.eye[1]));
      fly = { t0: performance.now(), ms, kind: 'eye', p0: eyePos.clone(), p1,
        y0: look.yaw, y1: look.yaw + shortest(look.yaw, l1.yaw), x0: look.pitch, x1: l1.pitch };
    }
  }

  function setMode(next) {
    if (next === mode) return mode;
    mode = next;
    controls.enabled = mode === 'orbit';
    if (mode === 'eye') {
      const v = V()[current] || V().overview;
      eyePos.set(...v.eye[0]);
      Object.assign(look, lookFrom(eyePos, new THREE.Vector3(...v.eye[1])));
      fly = null;
      camera.fov = 62;
    } else {
      camera.fov = 40;
      view(current, 1200);
    }
    camera.updateProjectionMatrix();
    container.dispatchEvent(new CustomEvent('cameramode', { detail: mode }));
    return mode;
  }

  // Осмотр мышью и шаги клавишами — только от первого лица.
  let drag = null;
  renderer.domElement.addEventListener('pointerdown', (e) => {
    if (mode !== 'eye') return;
    drag = { x: e.clientX, y: e.clientY, yaw: look.yaw, pitch: look.pitch };
    renderer.domElement.setPointerCapture(e.pointerId);
  });
  renderer.domElement.addEventListener('pointermove', (e) => {
    if (!drag) return;
    fly = null;
    look.yaw = drag.yaw + (e.clientX - drag.x) * 0.004;
    look.pitch = Math.max(-1.2, Math.min(1.2, drag.pitch + (e.clientY - drag.y) * 0.004));
  });
  renderer.domElement.addEventListener('pointerup', () => { drag = null; });
  const keys = new Set();
  const MOVE = { KeyW: [0, 1], ArrowUp: [0, 1], KeyS: [0, -1], ArrowDown: [0, -1], KeyA: [-1, 0], ArrowLeft: [-1, 0], KeyD: [1, 0], ArrowRight: [1, 0] };
  addEventListener('keydown', (e) => {
    if (mode !== 'eye' || !MOVE[e.code] || /INPUT|TEXTAREA|SELECT/.test(e.target.tagName)) return;
    keys.add(e.code); e.preventDefault(); e.stopPropagation();
  }, true);
  addEventListener('keyup', (e) => keys.delete(e.code), true);
  addEventListener('blur', () => keys.clear());

  function walk(dt) {
    if (!keys.size) return;
    let f = 0, s = 0;
    for (const k of keys) { f += MOVE[k][1]; s += MOVE[k][0]; }
    if (!f && !s) return;
    fly = null;
    const sp = 1.6 * dt / Math.hypot(f, s);
    const fwd = new THREE.Vector3(-Math.sin(look.yaw), 0, -Math.cos(look.yaw));
    const right = new THREE.Vector3(-fwd.z, 0, fwd.x);
    const next = eyePos.clone().addScaledVector(fwd, f * sp).addScaledVector(right, s * sp);
    const feet = eyePos.y - EYE;
    const y = floorAt(next.x, next.z, feet);
    if (y === null || Math.abs(y - feet) > 0.45) return;
    eyePos.set(next.x, y + EYE, next.z);
  }

  // --- анимации узлов ---------------------------------------------------------------
  const tweens = [];
  let timeScale = 1;
  const tween = (ms, fn) => new Promise((res) => tweens.push({ t0: performance.now(), ms: ms / timeScale, fn, res }));
  const lerp = (a, b, t) => a + (b - a) * t;

  const slideTop = pipeBox().max.y;
  const tankBottomY = () => worldOf(tank).y + 1.58 * tank.scale.y + 0.15;   // дно котла (локально 1,58)
  const HATCH_OPEN = 1.95;
  let swing = 0, tiltA = 0;

  const state = {
    reset() {
      // недоигранные анимации снимаются: их ожидание завершается, сцена — в исходном
      tweens.splice(0).forEach((t) => t.res());
      hinge.quaternion.identity();
      swing = 0; tiltA = 0; setArm(0, 0);
      slide.position.copy(slideBase);
      tank.position.z = tankStartZ;
      placeShoes(tankStartZ);
      buildCable(tankParkZ);
      lampGlow.intensity = 0;
      lampMesh.material = lampOff; if (tints.has(lampMesh)) tints.set(lampMesh, lampOff);
      tankMats.forEach((m) => { m.transparent = false; m.opacity = 1; m.depthWrite = true; m.needsUpdate = true; });
    },
    // цистерна заезжает под стояк из своей точки в уровне
    async arrive(ms = 5200) {
      shoes.forEach((s) => { s.visible = false; });
      const z0 = tank.position.z;
      await tween(ms, (t) => { tank.position.z = lerp(z0, tankParkZ, ease(t)); });
      placeShoes(tankParkZ);
    },
    park() { tank.position.z = tankParkZ; placeShoes(tankParkZ); },
    async grounding() {
      cableMesh.visible = true;
      await tween(500, () => {});
      lampMesh.material = lampOn; if (tints.has(lampMesh)) tints.set(lampMesh, lampOn);
      await tween(500, (t) => { lampGlow.intensity = 0.8 * t; });
    },
    // крышка откидывается вверх, на шарнир (в обратную сторону она уходила бы в котёл)
    async hatch() { await tween(1600, (t) => { hinge.quaternion.setFromAxisAngle(hingeAxis, HATCH_OPEN * ease(t)); }); },
    async install() {
      // Сложенный наконечник висит ниже верха котла: плечо сначала поднимают,
      // потом заводят над горловиной и только потом опускают — иначе труба
      // прошла бы сквозь цистерну.
      await tween(1300, (t) => { tiltA = lerp(0, LIFT, ease(t)); setArm(swing, tiltA); });
      await tween(2400, (t) => { swing = lerp(0, SWING, ease(t)); setArm(swing, tiltA); });
      await tween(2200, (t) => { tiltA = lerp(LIFT, TILT, ease(t)); setArm(swing, tiltA); });
    },
    async clamp() {
      // груз механизма прижатия дожимает крышку наконечника к горловине
      const a0 = tiltA;
      await tween(900, (t) => { tiltA = lerp(a0, TILT + 0.012, ease(t)); setArm(swing, tiltA); });
    },
    async bottom() {
      tankMats.forEach((m) => { m.transparent = true; m.depthWrite = false; m.needsUpdate = true; });
      await tween(700, (t) => tankMats.forEach((m) => { m.opacity = lerp(1, 0.3, t); }));
      scene.updateMatrixWorld(true);
      const depth = pipeBox().min.y - tankBottomY();
      await tween(4200, (t) => { slide.position.copy(slideBase).addScaledVector(slideDown, depth * ease(t)); });
    },
  };

  // --- цикл ---------------------------------------------------------------------------
  function resize() {
    const w = container.clientWidth, h = container.clientHeight;
    renderer.setSize(w, h, false);
    composer.setSize(w, h);
    camera.aspect = w / h; camera.updateProjectionMatrix();
  }
  new ResizeObserver(resize).observe(container);
  resize();

  let pausedAt = null;
  // Кадр живёт: камера чуть покачивается вокруг точки, где закончился
  // перелёт, а не уходит по кругу — иначе узкие ракурсы (заземление между
  // цистерной и эстакадой) за полминуты упираются в борт настила.
  let idle = null;
  const settle = (now) => { idle = { off: camera.position.clone().sub(controls.target), t0: now }; };
  controls.addEventListener('start', () => { idle = null; });
  let last = performance.now();
  renderer.setAnimationLoop((now) => {
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    if (pausedAt === null) {
      if (fly) {
        const t = Math.min(1, (now - fly.t0) / fly.ms), e = ease(t);
        if (fly.kind === 'orbit') {
          // Сначала вверх, потом в сторону, и дугой: по прямой камера из
          // прохода у заземления прошла бы сквозь цистерну.
          const ey = 1 - Math.pow(1 - e, 3), exz = Math.pow(e, 1.7);
          camera.position.set(lerp(fly.p0.x, fly.p1.x, exz), lerp(fly.p0.y, fly.p1.y, ey), lerp(fly.p0.z, fly.p1.z, exz));
          camera.position.y += Math.sin(Math.PI * e) * Math.min(4, fly.p0.distanceTo(fly.p1) * 0.3);
          controls.target.lerpVectors(fly.q0, fly.q1, e);
        } else {
          eyePos.lerpVectors(fly.p0, fly.p1, e);
          look.yaw = lerp(fly.y0, fly.y1, e); look.pitch = lerp(fly.x0, fly.x1, e);
        }
        if (t >= 1) { fly = null; if (mode === 'orbit') settle(now); }
      } else if (mode === 'orbit' && idle) {
        const a = Math.sin((now - idle.t0) / 5000) * 0.05;
        camera.position.copy(controls.target).add(idle.off.clone().applyAxisAngle(UP, a));
      }
      for (let i = tweens.length - 1; i >= 0; i--) {
        const tw = tweens[i];
        const t = Math.min(1, (now - tw.t0) / tw.ms);
        tw.fn(t);
        if (t >= 1) { tweens.splice(i, 1); tw.res(); }
      }
    }
    if (mode === 'eye') {
      walk(dt);
      camera.position.copy(eyePos);
      camera.quaternion.setFromEuler(new THREE.Euler(look.pitch, look.yaw, 0, 'YXZ'));
    } else {
      controls.update();
    }
    const pulse = 0.5 + 0.5 * Math.sin(now / 260);
    for (const [m] of tints) if (m.material.emissive && m !== lampMesh) m.material.emissiveIntensity = 0.25 + 0.9 * pulse;
    composer.render();
  });

  state.reset();
  camera.position.set(...V().overview.orbit[0]);
  controls.target.set(...V().overview.orbit[1]);
  settle(performance.now());
  eyePos.set(...V().overview.eye[0]);

  return {
    camera, controls, parts, renderer, scene,
    debug: { actors, nodes, tankParkZ, pipeOffX, seatY, TILT, LIFT, deckY, neck: () => P.hatch() },
    focus(name, color = 0x19e3d6, flyTo = true) {
      const p = parts[name];
      outline.selectedObjects = p.meshes;
      outline.visibleEdgeColor.set(color);
      outline.hiddenEdgeColor.set(new THREE.Color(color).multiplyScalar(0.45));
      setTint(p.meshes, color);
      if (flyTo) view(name);
    },
    clear() { outline.selectedObjects = []; setTint([], 0); },
    // пока оператор без каски, анимации узлов и камера стоят
    pause() { if (pausedAt === null) pausedAt = performance.now(); },
    resume() {
      if (pausedAt === null) return;
      const dt = performance.now() - pausedAt;
      pausedAt = null;
      tweens.forEach((t) => { t.t0 += dt; });
      if (fly) fly.t0 += dt;
    },
    view,
    get mode() { return mode; },
    setMode,
    toggleMode() { return setMode(mode === 'orbit' ? 'eye' : 'orbit'); },
    // шаг назад на пульте: операции до нужного этапа проигрываются мгновенно
    speed(k) { timeScale = k; },
    xray(on) {
      tankMats.forEach((m) => { m.transparent = on; m.opacity = on ? 0.3 : 1; m.depthWrite = !on; m.needsUpdate = true; });
    },
    anim: state,
  };
}
