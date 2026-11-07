"""Минимальный читатель редакторских .uasset/.umap UE4 (FileVersionUE4 = 522).

Читает таблицы имён/импортов/экспортов и теговые свойства экспортов.
Значения разбираются для простых типов и структур Vector/Rotator/Quat/Transform;
остальное отдаётся сырыми байтами.
"""
import struct, sys, json


class R:
    def __init__(self, b, p=0):
        self.b, self.p = b, p

    def u(self, fmt):
        v = struct.unpack_from('<' + fmt, self.b, self.p)
        self.p += struct.calcsize('<' + fmt)
        return v if len(v) > 1 else v[0]

    def i32(self): return self.u('i')
    def u32(self): return self.u('I')
    def i64(self): return self.u('q')

    def fstr(self):
        n = self.i32()
        if n == 0: return ''
        if n > 0:
            s = self.b[self.p:self.p + n]; self.p += n
            return s[:-1].decode('latin-1')
        n = -n
        s = self.b[self.p:self.p + 2 * n]; self.p += 2 * n
        return s[:-2].decode('utf-16-le')


class Package:
    def __init__(self, path):
        self.path = path
        b = open(path, 'rb').read()
        self.b = b
        r = R(b)
        tag = r.u32(); assert tag == 0x9E2A83C1, hex(tag)
        legacy = r.i32()
        if legacy != -4: r.i32()  # LegacyUE3Version
        self.ver = r.i32(); r.i32()
        if legacy <= -2:
            n = r.i32(); r.p += n * 20
        self.header_size = r.i32()
        self.folder = r.fstr()
        self.pkg_flags = r.u32()
        name_count, name_off = r.i32(), r.i32()
        if self.ver >= 516: r.fstr()  # LocalizationId (только редакторские пакеты)
        r.i32(); r.i32()  # GatherableTextData
        exp_count, exp_off = r.i32(), r.i32()
        imp_count, imp_off = r.i32(), r.i32()

        r.p = name_off
        self.names = []
        for _ in range(name_count):
            self.names.append(r.fstr()); r.p += 4

        r.p = imp_off
        self.imports = []
        for _ in range(imp_count):
            cp = self.fname(r); cn = self.fname(r); outer = r.i32(); on = self.fname(r)
            if self.ver >= 520 and not (self.pkg_flags & 0x80000000): self.fname(r)  # PackageName
            self.imports.append({'class_pkg': cp, 'class': cn, 'outer': outer, 'name': on})

        r.p = exp_off
        self.exports = []
        for _ in range(exp_count):
            e = {}
            e['class'] = r.i32(); e['super'] = r.i32(); e['template'] = r.i32(); e['outer'] = r.i32()
            e['name'] = self.fname(r); e['flags'] = r.u32()
            e['size'] = r.i64(); e['offset'] = r.i64()
            r.p += 4 * 3 + 16 + 4 + 4 + 4 + 4 * 5
            self.exports.append(e)

    def fname(self, r):
        i, n = r.i32(), r.i32()
        s = self.names[i]
        return s if n == 0 else f'{s}_{n - 1}'

    def ref(self, idx):
        if idx == 0: return None
        if idx < 0: return self.imports[-idx - 1]['name']
        return self.exports[idx - 1]['name']

    def ref_class(self, idx):
        if idx == 0: return None
        if idx < 0: return self.imports[-idx - 1]['class']
        return self.ref(self.exports[idx - 1]['class'])

    def path_of(self, idx):
        """Полный путь импорта: /Game/...Asset.Asset"""
        parts = []
        while idx != 0:
            if idx < 0:
                im = self.imports[-idx - 1]; parts.append(im['name']); idx = im['outer']
            else:
                ex = self.exports[idx - 1]; parts.append(ex['name']); idx = ex['outer']
        return '.'.join(reversed(parts))

    def export_class(self, e):
        return self.ref(e['class'])

    def props(self, e, limit=None):
        r = R(self.b, e['offset'])
        end = e['offset'] + e['size']
        return self.read_props(r, end)

    def read_props(self, r, end):
        out = {}
        while r.p < end:
            name = self.fname(r)
            if name == 'None': break
            typ = self.fname(r); size = r.i32(); aidx = r.i32()
            extra = None
            if typ == 'StructProperty':
                extra = self.fname(r); r.p += 16
            elif typ == 'BoolProperty':
                extra = r.u('B')
            elif typ in ('ByteProperty', 'EnumProperty'):
                extra = self.fname(r)
            elif typ in ('ArrayProperty', 'SetProperty'):
                extra = self.fname(r)
            elif typ == 'MapProperty':
                extra = (self.fname(r), self.fname(r))
            if r.u('B'): r.p += 16
            start = r.p
            val = self.value(r, typ, size, extra)
            r.p = start + size
            key = name if aidx == 0 else f'{name}[{aidx}]'
            out[key] = val
        return out

    def value(self, r, typ, size, extra):
        if typ == 'BoolProperty': return bool(extra)
        if typ == 'FloatProperty': return r.u('f')
        if typ == 'DoubleProperty': return r.u('d')
        if typ == 'IntProperty': return r.i32()
        if typ == 'NameProperty': return self.fname(r)
        if typ == 'StrProperty': return r.fstr()
        if typ in ('ObjectProperty', 'ClassProperty', 'WeakObjectProperty'):
            i = r.i32(); return {'ref': i, 'name': self.ref(i)}
        if typ == 'SoftObjectProperty':
            p = self.fname(r); s = r.fstr(); return p
        if typ in ('ByteProperty', 'EnumProperty'):
            if size == 1: return r.u('B')
            return self.fname(r)
        if typ == 'StructProperty':
            if extra in ('Vector',): return list(r.u('fff'))
            if extra == 'Rotator': return list(r.u('fff'))
            if extra in ('Quat', 'Vector4', 'Plane'): return list(r.u('ffff'))
            if extra == 'LinearColor': return list(r.u('ffff'))
            if extra == 'Color': return list(r.u('BBBB'))
            if extra == 'Vector2D': return list(r.u('ff'))
            if extra == 'Guid': return r.b[r.p:r.p + 16].hex()
            try:
                return {'_struct': extra, **self.read_props(r, r.p + size)}
            except Exception:
                return {'_struct': extra, '_raw': size}
        if typ == 'ArrayProperty':
            n = r.i32()
            if extra == 'ObjectProperty':
                return [{'ref': (i := r.i32()), 'name': self.ref(i)} for _ in range(n)]
            if extra == 'StructProperty':
                # заголовок внутреннего тега
                self.fname(r); self.fname(r); r.i32(); r.i32()
                sname = self.fname(r); r.p += 16; r.p += 1
                items = []
                native = {'Vector': 'fff', 'Rotator': 'fff', 'Quat': 'ffff', 'LinearColor': 'ffff',
                          'Vector2D': 'ff', 'Color': 'BBBB'}
                try:
                    for _ in range(n):
                        if sname in native: items.append(list(r.u(native[sname])))
                        elif sname == 'Guid': items.append(r.b[r.p:r.p + 16].hex()); r.p += 16
                        else: items.append(self.read_props(r, len(r.b)))
                except (IndexError, struct.error):
                    return {'_struct': sname, 'items': items, '_partial': True}
                return {'_struct': sname, 'items': items}
            if extra == 'NameProperty':
                return [self.fname(r) for _ in range(n)]
            if extra == 'FloatProperty':
                return [r.u('f') for _ in range(n)]
            if extra == 'IntProperty':
                return [r.i32() for _ in range(n)]
            return {'_array': extra, 'n': n}
        return {'_type': typ, '_size': size}


if __name__ == '__main__':
    p = Package(sys.argv[1])
    print('ver', p.ver, 'names', len(p.names), 'imports', len(p.imports), 'exports', len(p.exports))
    for i, e in enumerate(p.exports, 1):
        print(i, p.export_class(e), e['name'], 'outer=', p.ref(e['outer']))
