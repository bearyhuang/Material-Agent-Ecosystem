#!/usr/bin/env python3
"""原料分子 3D 预生成 + 多层标注（供前端 3Dmol 渲染，离线可用）。

为什么是"预生成"而不是前端实时算：
  * 前端没有 RDKit，SMILES → 3D 坐标需要 ETKDG 力场优化，浏览器做不到；
  * 预生成后前端只消费 JSON，**断网也能演示**。

产出：web/data/molecules.json
  { code, name, category, smiles, molblock, atoms:[{e,x,y,z}],
    bonds:[{i,j,order,aromatic}],
    groups:[{name, smarts, atoms:[idx], color}],
    formula, mw, role, properties:{...} }

多层标注对应"材料计算中心"：
  · 特性(properties/role)  · 化学键(bonds)  · 官能团(groups)  · 性能(运行时经 /api/material/{code}/impact 拉取)

用法：docker compose exec -T arena python scripts/gen_molecules.py
"""
import json
import re
from pathlib import Path

from rdkit import Chem
from rdkit.Chem import AllChem

ROOT = Path(__file__).resolve().parent.parent          # backend/
OUT = ROOT.parent / "frontend" / "data" / "molecules.json"

# 代表结构 SMILES（真实工业牌号多为聚合/混合物，这里取其**代表性结构单元**用于教学与可视化；
# 这一点必须在页面上声明，避免被误读为精确结构）
SMILES = {
    # ---- 环氧体系 ----
    "E51":     "CC(C)(c1ccc(OCC2CO2)cc1)c1ccc(OCC2CO2)cc1",
    "E44":     "CC(C)(c1ccc(OCC2CO2)cc1)c1ccc(OCC2CO2)cc1",
    "DER331":  "CC(C)(c1ccc(OCC2CO2)cc1)c1ccc(OCC2CO2)cc1",
    "F51":     "c1ccc(cc1)CC1=CC=C(C=C1)OCC1CO1",
    "TGDDM":   "CN(CC1CO1)CC1CO1",
    "BGE":     "CCCCOCC1CO1",
    "AGE":     "CCCCCCCCCCCCOCC1CO1",
    "DETA":    "NCCNCCN",
    "TETA":    "NCCNCCNCCN",
    "IPDA":    "CC1(C)CC(N)CC(C)(CN)C1",
    "DDS":     "Nc1ccc(S(=O)(=O)c2ccc(N)cc2)cc1",
    "DICY":    "N=C(N)NC#N",
    "MTHPA":   "CC1CCC2C(C1)C(=O)OC2=O",
    "PA650":   "CCCCCCCCCCCCCCCC(=O)NCCN",
    "MDI":     "O=C=Nc1ccc(Cc2ccc(N=C=O)cc2)cc1",
    "PPG2000": "CC(O)COCC(C)OCC(C)O",
    "KH560":   "CO[Si](OC)(OC)CCCOCC1CO1",
    "DMP30":   "CN(C)Cc1cc(CN(C)C)c(O)c(CN(C)C)c1",
    "SILICA":  "O=[Si]=O",
    "CACO3":   "O=C([O-])[O-].[Ca+2]",
    # ---- 光刻胶体系 ----
    "PHS-PROT":   "Oc1ccc(C=C)cc1",
    "NOVOLAC-MC": "Cc1ccc(O)cc1",
    "ACRYL-LAC":  "CC(=C)C(=O)OC1CCCCO1",
    "TPS-NF": "c1ccc([S+](c2ccccc2)c2ccccc2)cc1.O=S(=O)([O-])C(F)(F)C(F)(F)C(F)(F)F",
    "TPS-TF": "c1ccc([S+](c2ccccc2)c2ccccc2)cc1.O=S(=O)([O-])C(F)(F)F",
    "DNQ-PAC":    "O=S(=O)(O)c1ccccc1",
    "TBAH":       "CCCC[N+](CCCC)(CCCC)CCCC.[OH-]",
    "TEA-OH":     "OCCN(CCO)CCO",
    "PGMEA":      "COCC(C)OC(C)=O",
    "EL":         "CCOC(=O)C(O)C",
    "SURF-FC":    "FC(F)(F)C(F)(F)C(F)(F)C(=O)O",
    "ADH-SILANE": "CO[Si](OC)(OC)CCCN",
}

# 官能团标注（SMARTS）：前端据此高亮原子，回答"哪个原子在起作用"
GROUPS = [
    ("环氧基", "[CH2]1[O][CH]1", "#e74c3c"),
    ("环氧基", "C1OC1", "#e74c3c"),
    ("伯/仲胺", "[NX3;H2,H1;!$(N[C,S]=[O,S,N])]", "#3498db"),
    ("酸酐", "C(=O)OC(=O)", "#9b59b6"),
    ("异氰酸酯", "N=C=O", "#e67e22"),
    ("砜基", "S(=O)(=O)", "#16a085"),
    ("羟基", "[OX2H]", "#27ae60"),
    ("硅氧烷", "[Si]", "#7f8c8d"),
    ("酯基", "C(=O)O", "#d35400"),
    ("羧酸", "C(=O)[OX2H1]", "#c0392b"),
    ("苯环", "c1ccccc1", "#95a5a6"),
    ("三氟甲基", "C(F)(F)F", "#f1c40f"),
    ("磺酸盐", "S(=O)(=O)[O-]", "#8e44ad"),
    ("锍阳离子", "[S+]", "#2980b9"),
    ("季铵阳离子", "[N+]", "#2980b9"),
    ("腈基", "C#N", "#34495e"),
]

# 每种材料的化学角色说明（面向"材料计算中心"的"特性"卡片）
ROLE = {
    "E51": "双酚 A 型环氧树脂（DGEBA），环氧体系主树脂，提供环氧基交联点。",
    "E44": "中等分子量双酚 A 环氧树脂，黏度低于 E51，便于工艺。",
    "DER331": "标准双酚 A 环氧树脂，与 E51 同源。",
    "F51": "酚醛环氧树脂，多环氧官能度，耐热性优于 DGEBA。",
    "TGDDM": "四缩水甘油二氨基二苯甲烷，高官能度耐热环氧，用于航空级胶。",
    "BGE": "丁基缩水甘油醚，单官能度活性稀释剂，降黏度。",
    "AGE": "长链烷基缩水甘油醚，柔性稀释剂。",
    "DETA": "二乙烯三胺，室温固化剂，伯/仲胺活泼氢。",
    "TETA": "三乙烯四胺，多胺室温固化剂。",
    "IPDA": "异佛尔酮二胺，脂环胺，耐候/耐黄变固化剂。",
    "DDS": "4,4'-二氨基二苯砜，高温固化剂，耐热骨架。",
    "DICY": "双氰胺，潜伏性固化剂，需高温触发。",
    "MTHPA": "甲基四氢邻苯二甲酸酐，酸酐固化剂，低挥发性。",
    "PA650": "聚酰胺固化剂，柔性长链。",
    "MDI": "二苯基甲烷二异氰酸酯，聚氨酯主原料。",
    "PPG2000": "聚丙二醇，聚氨酯软段/扩链。",
    "KH560": "γ-缩水甘油醚氧丙基三甲氧基硅烷，偶联剂，桥接有机/无机。",
    "DMP30": "2,4,6-三(二甲氨基甲基)苯酚，叔胺促进剂。",
    "SILICA": "二氧化硅填料，触变/增强。",
    "CACO3": "碳酸钙填料，低成本增量。",
    "PHS-PROT": "对羟基苯乙烯树脂（PHS），KrF 光刻胶骨架，苯环提供刻蚀抗性。",
    "NOVOLAC-MC": "甲酚酚醛树脂，g/i-line 光刻胶骨架。",
    "ACRYL-LAC": "丙烯酸内酯骨架，ArF 光刻胶用透明骨架。",
    "TPS-NF": "三苯基硫鎓非aflate 盐，化学放大胶产酸剂（PAG）。",
    "TPS-TF": "三苯基硫鎓三氟甲磺酸盐，PAG，强酸产酸。",
    "DNQ-PAC": "重氮萘醌-磺酸（DNQ），g/i-line 感光剂。",
    "TBAH": "四丁基氢氧化铵，碱性淬灭剂（淬火酸扩散）。",
    "TEA-OH": "乙醇胺，碱性淬灭剂。",
    "PGMEA": "丙二醇甲醚醋酸酯，光刻胶标准溶剂。",
    "EL": "乳酸乙酯，环保型光刻胶溶剂。",
    "SURF-FC": "含氟表面活性剂，降低表面张力改善涂布。",
    "ADH-SILANE": "氨基硅烷偶联剂，提升附着力。",
}

# 能被前端当作"数值特性"展示的 raw_material.props 键（其余过滤）
NUMERIC_KEYS = {"mw", "tg", "eew", "ahew", "nco_index", "acid_value",
                "molar_mass", "boiling_point", "viscosity"}


def build(code, smiles):
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None, "SMILES 解析失败"
    mol = Chem.AddHs(mol)
    if AllChem.EmbedMolecule(mol, randomSeed=0xf00d) != 0:
        return None, "3D 嵌入失败"
    try:
        AllChem.MMFFOptimizeMolecule(mol)
    except Exception:
        pass
    conf = mol.GetConformer()

    atoms = [{"e": a.GetSymbol(),
              "x": round(float(conf.GetAtomPosition(i).x), 3),
              "y": round(float(conf.GetAtomPosition(i).y), 3),
              "z": round(float(conf.GetAtomPosition(i).z), 3)}
             for i, a in enumerate(mol.GetAtoms())]

    bonds = []
    for b in mol.GetBonds():
        bonds.append({"i": b.GetBeginAtomIdx(), "j": b.GetEndAtomIdx(),
                      "order": int(b.GetBondTypeAsDouble()),
                      "aromatic": bool(b.GetIsAromatic())})

    groups, seen = [], set()
    for gname, smarts, color in GROUPS:
        patt = Chem.MolFromSmarts(smarts)
        if patt is None:
            continue
        for match in mol.GetSubstructMatches(patt):
            key = (gname, tuple(sorted(match)))
            if key in seen:
                continue
            seen.add(key)
            groups.append({"name": gname, "smarts": smarts,
                           "atoms": list(match), "color": color})

    molblock = Chem.MolToMolBlock(mol)
    return {
        "code": code, "smiles": smiles, "molblock": molblock,
        "atoms": atoms, "bonds": bonds, "groups": groups[:24],
        "formula": Chem.rdMolDescriptors.CalcMolFormula(mol),
        "mw": round(Chem.rdMolDescriptors.CalcExactMolWt(mol), 2),
        "role": ROLE.get(code, ""),
    }, None


def load_props():
    """从库里拉取每种原料的 props / 类别 / 名称（用于"特性"卡片）。无库则跳过。"""
    try:
        import sys
        sys.path.insert(0, str(ROOT / "src"))
        from fsdb import connect
        out = {}
        with connect() as c, c.cursor() as cur:
            cur.execute("select code, name, category, props from mat.raw_material")
            for r in cur.fetchall():
                # 注意：fsdb 默认用 dict_row，不能当元组解包（会解出列名而非值）
                code, name, cat, props = r["code"], r["name"], r["category"], r["props"]
                if isinstance(props, str):
                    try:
                        props = json.loads(props)
                    except Exception:
                        props = {}
                out[code] = {"name": name, "category": cat,
                             "props": props or {}}
        return out
    except Exception as e:
        print("  [warn] 拉取原料 props 失败（将仅用内嵌角色说明）:", e)
        return {}


def main():
    db = load_props()
    out, bad = [], []
    for code, smi in SMILES.items():
        d, err = build(code, smi)
        if not d:
            bad.append((code, err))
            continue
        meta = db.get(code, {})
        props = meta.get("props", {}) or {}
        # 展示所有数值型属性（分子量/玻璃化温度/Tg/当量/黏度…），键名随库而定
        numeric = {k: v for k, v in props.items()
                   if isinstance(v, (int, float)) and not isinstance(v, bool)}
        for k in list(NUMERIC_KEYS):
            if k in props and k not in numeric:
                numeric[k] = props[k]
        d["name"] = meta.get("name") or code
        d["category"] = meta.get("category") or ""
        d["properties"] = numeric
        out.append(d)

    payload = {
        "note": "代表结构单元，用于教学与可视化；工业牌号多为聚合/混合物，不等于精确结构。"
                "3D 坐标由 RDKit ETKDG + MMFF 预生成，离线可用。化学键/官能团由程序自动标注。",
        "legend": {
            "order": {1: "单键", 2: "双键", 3: "三键", 4: "芳香键(共振)"},
            "groups_color": {name: color for name, _, color in GROUPS},
        },
        "molecules": out,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    print(f"已生成 {OUT}：{len(out)} 个分子（含化学键/官能团/角色），失败 {len(bad)}")
    for c, e in bad:
        print("  失败:", c, e)


if __name__ == "__main__":
    main()
