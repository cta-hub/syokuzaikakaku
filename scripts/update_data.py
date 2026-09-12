#!/usr/bin/env python3
"""index.html の基準データを最新に書き換える。
  ・札幌市「生活関連商品小売価格調査」PDF → 定番16品目（BASE）
  ・総務省「小売物価統計調査」e-Stat Excel → 検索用の品目（ESTAT）※環境変数 ESTAT_APP_ID が必要

GitHub Actions から毎日呼ばれる（.github/workflows/update-and-deploy.yml）。

  python scripts/update_data.py            # 新しいPDFが出ていれば取り込む
  python scripts/update_data.py --verify   # 最新PDFが取り込み済みでも解析し、埋め込み値と一致するか検証
  python scripts/update_data.py --no-fetch # 通信せず、sw.js の VERSION だけ再計算

安全装置:
  - 平均・最安・最高の整合（最安 <= 平均 <= 最高）が崩れていたら中止
  - 前回の平均から 0.4倍〜2.5倍 を外れる品目があったら中止（読み取りミス対策）
  - 中止時は終了コード1 → Actions が失敗扱い → GitHub からメール通知が届く
  - 札幌市サイトに一時的につながらないだけなら警告のみで正常終了（毎日の誤通知を防ぐ）
"""
import argparse
import hashlib
import io
import json
import re
import sys
import unicodedata
import urllib.request
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent
INDEX = ROOT / "index.html"
MANIFEST = ROOT / "manifest.json"
SW = ROOT / "sw.js"

LIST_URL = "https://www.city.sapporo.jp/shohi/01-shohi/05-information/seikatsu.html"
UA = "Mozilla/5.0 (X11; Linux x86_64) kaidoki-updater/1.0"

# アプリの品目ID → PDF上の (品目名, 規格の末尾)。PDFの表記に合わせる（ひらがな表記に注意）
ROWS = {
    "cabbage": ("きゃべつ", "100g"),
    "onion": ("玉ねぎ", "100g"),
    "daikon": ("だいこん", "100g"),
    "hakusai": ("はくさい", "100g"),
    "lettuce": ("レタス", "100g"),
    "carrot": ("にんじん", "100g"),
    "potato": ("ばれいしょ", "100g"),
    "tomato": ("トマト", "100g"),
    "cucumber": ("きゅうり", "100g"),
    "negi": ("長ねぎ", "100g"),
    "spinach": ("ほうれん草", "100g"),
    "shiitake": ("生しいたけ", "100g"),
    "chicken": ("鶏肉", "100g"),
    "pork": ("豚肉", "100g"),
    "egg": ("鶏卵", "1ケース"),
    "milk": ("牛乳", "1本"),
}
RATIO_MIN, RATIO_MAX = 0.4, 2.5

DATA_RE = re.compile(r"/\*DATA-BEGIN\*/(.*?)/\*DATA-END\*/", re.S)
VERSION_RE = re.compile(r"^const VERSION = '[^']*';", re.M)
NUM = r"\d{1,3}(?:,\d{3})+|\d+"


class NetError(Exception):
    pass


class ParseError(Exception):
    pass


def http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.read()
    except Exception as e:  # noqa: BLE001
        raise NetError(f"{url} に接続できません: {e}") from e


def find_latest_pdf(html: str, base: str = LIST_URL):
    """一覧ページから『YYYYMMDD_...kouri...pdf』形式のリンクを集め、日付が最新のものを返す。"""
    cands = []
    for href in re.findall(r'href="([^"]+?\.pdf)"', html, re.I):
        name = href.rsplit("/", 1)[-1]
        m = re.match(r"(\d{8})_", name)
        if m and "kouri" in name.lower():
            cands.append((m.group(1), urljoin(base, href), name))
    if not cands:
        raise ParseError("一覧ページに調査結果PDFのリンクが見つかりません（ページ構成が変わった可能性）")
    return max(cands)


def pdf_text(data: bytes) -> str:
    import pdfplumber

    with pdfplumber.open(io.BytesIO(data)) as pdf:
        return "\n".join((p.extract_text() or "") for p in pdf.pages)


def _spaced(s: str) -> str:
    """文字間に空白・改行が挟まっても一致する正規表現にする（PDF抽出でよく起きる）"""
    return r"\s*".join(re.escape(ch) for ch in s)


def parse_prices(text: str) -> dict:
    t = unicodedata.normalize("NFKC", text)  # 全角数字・全角g・ℓ などを半角に
    out, missing = {}, []
    for key, (name, spec) in ROWS.items():
        pat = re.compile(
            _spaced(name) + r"[\s\S]{0,40}?" + _spaced(spec) + r"((?:\s*円?\s*(?:" + NUM + r")){3})"
        )
        m = pat.search(t)
        if not m:
            missing.append(name)
            continue
        nums = [int(x.replace(",", "")) for x in re.findall(NUM, m.group(1))][:3]
        # 表の列順（平均/最高/最低 など）に依存しないよう、3つの中央値を平均とみなす
        lo, avg, hi = sorted(nums)
        out[key] = {"avg": avg, "min": lo, "max": hi}
    if missing:
        raise ParseError("PDFから読み取れなかった品目: " + "、".join(missing))
    return out


def parse_period(text: str, date8: str) -> str:
    t = unicodedata.normalize("NFKC", text)
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d+)\s*月", t)
    if m:
        return f"{2018 + int(m.group(1))}-{int(m.group(2)):02d}"
    return f"{date8[:4]}-{date8[4:6]}"


def validate(new: dict, old: dict) -> None:
    errs = []
    for k, v in new.items():
        if not (0 < v["min"] <= v["avg"] <= v["max"]):
            errs.append(f"{k}: 最安/平均/最高 の整合が取れない {v}")
        prev = old.get(k, {}).get("avg")
        if prev and not (RATIO_MIN <= v["avg"] / prev <= RATIO_MAX):
            errs.append(f"{k}: 平均が前回 {prev} → {v['avg']} と極端に変化（読み取りミスの可能性）")
    if errs:
        raise ParseError("検証エラー:\n  " + "\n  ".join(errs))


def load_data(html: str):
    m = DATA_RE.search(html)
    if not m:
        raise ParseError("index.html に /*DATA-BEGIN*/ 〜 /*DATA-END*/ が見つかりません")
    return json.loads(m.group(1))


def write_data(html: str, data: dict) -> str:
    body = json.dumps(data, ensure_ascii=False, indent=2)
    # prices の各品目は1行にまとめて読みやすく
    body = re.sub(r'\{\s*"avg": (\d+),\s*"min": (\d+),\s*"max": (\d+)\s*\}',
                  r'{"avg": \1, "min": \2, "max": \3}', body)
    return DATA_RE.sub(lambda _: "/*DATA-BEGIN*/" + body + "/*DATA-END*/", html, count=1)


def bump_version() -> bool:
    """index.html と manifest.json の中身から版数を作り sw.js に書く。中身が変われば版数も変わる。"""
    h = hashlib.sha256(INDEX.read_bytes() + MANIFEST.read_bytes()).hexdigest()[:10]
    sw = SW.read_text(encoding="utf-8")
    new = VERSION_RE.sub(f"const VERSION = '{h}';", sw, count=1)
    if new != sw:
        SW.write_text(new, encoding="utf-8", newline="\n")
        print(f"sw.js VERSION -> {h}")
        return True
    return False


def fetch_and_update(verify: bool) -> bool:
    html = INDEX.read_text(encoding="utf-8")
    data = load_data(html)

    listing = http_get(LIST_URL).decode("utf-8", "replace")
    date8, url, name = find_latest_pdf(listing)
    published = f"{date8[:4]}-{date8[4:6]}-{date8[6:]}"
    is_new = published > data["published"]
    print(f"最新PDF: {name}（公表 {published}）／取り込み済み: {data['published']}")

    if not is_new and not verify:
        print("新しいデータはありません")
        return False

    text = pdf_text(http_get(url))
    prices = parse_prices(text)
    period = parse_period(text, date8)
    validate(prices, data["prices"])

    if not is_new:  # --verify：取り込み済みの値とPDFの読み取り結果が一致するか
        diff = [f"{k}: 埋め込み {data['prices'][k]} / PDF {v}" for k, v in prices.items() if data["prices"].get(k) != v]
        if diff:
            raise ParseError("埋め込み値とPDFが一致しません:\n  " + "\n  ".join(diff))
        print("検証OK：埋め込み値とPDFの読み取り結果は一致")
        return False

    for k, v in prices.items():
        print(f"  {k:9s} {data['prices'].get(k, {}).get('avg', '-'):>5} → {v['avg']:>5}  ({v['min']}〜{v['max']})")
    data.update(period=period, published=published, pdf=name, prices=prices)
    INDEX.write_text(write_data(html, data), encoding="utf-8", newline="\n")
    print(f"index.html を {period} のデータに更新しました")
    return True


# =====================================================================
# 総務省 小売物価統計調査（e-Stat）… 検索で出てくる商品の基準値
#   1) e-Stat API「データカタログ」で『主要品目の都市別小売価格』の最新月のExcelを探す
#   2) Excelから「札幌市」の列を読み、品目・銘柄・数量単位・価格を取り出す
#   3) 数量単位（1kg, 100g, 1000ml …）を g / ml に換算できる物は100gあたりで比較できるようにする
# 必要なもの：環境変数 ESTAT_APP_ID（e-Stat のアプリケーションID。GitHub の Secrets に登録）
# =====================================================================
ESTAT_CATALOG = "https://api.e-stat.go.jp/rest/3.0/app/json/getDataCatalog"
ESTAT_RE = re.compile(r"/\*ESTAT-BEGIN\*/(.*?)/\*ESTAT-END\*/", re.S)
CITY = "札幌"
ESTAT_MIN_ITEMS = 50          # これより少なく読めたら表の形が想定外とみなして中止

# 定番16品目（札幌市30店データ）と重複する品目は検索に出さない。品目名の（ ）より前で判定
ESTAT_DUP = {
    "キャベツ", "たまねぎ", "玉ねぎ", "だいこん", "はくさい", "レタス", "にんじん", "じゃがいも", "ばれいしょ",
    "トマト", "きゅうり", "ねぎ", "ほうれんそう", "ほうれん草", "生しいたけ", "しいたけ", "鶏卵", "牛乳",
}
# よみがな自動生成（pykakasi）に加えて、口語の呼び方で引けるようにする別名
ESTAT_ALIAS = {
    "鶏卵": "たまご", "うるち米": "こめ おこめ", "もち米": "もちごめ", "食用油": "さらだあぶら あぶら",
    "しょう油": "しょうゆ 醤油", "食塩": "しお 塩", "豚肉": "ぶたにく", "牛肉": "ぎゅうにく", "鶏肉": "とりにく",
    "食パン": "ぱん", "豆腐": "とうふ", "油揚げ": "あぶらあげ", "納豆": "なっとう", "ティッシュ": "てぃっしゅ",
    "トイレットペーパー": "といれっとぺーぱー", "合いびき肉": "ひきにく", "ひき肉": "ひきにく",
}
# 食料品（品目符号1000〜2099：食料・飲料・酒類。2100番台以降の外食は除く）＋スーパーで買う日用品
FOOD_CODE = range(1000, 2100)
NONFOOD_KEYWORDS = ("ティッシュ", "トイレットペーパー", "洗剤", "柔軟剤", "ラップ", "ポリ袋", "ごみ袋",
                    "シャンプー", "リンス", "歯磨", "歯ブラシ", "石けん", "ボディソープ", "紙おむつ", "キッチンペーパー")

UNIT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(kg|g|ml|l)(?![a-z])")
UNIT_WORD_RE = re.compile(r"^\s*\d*\.?\d*\s*(kg|g|ml|l|個|本|袋|パック|枚|缶|箱|玉|尾|匹|束|組|足|丁|切|杯|瓶|ケース|セット|房|株|把)", re.I)


def as_list(x):
    return x if isinstance(x, list) else ([] if x is None else [x])


def _ym_from(text: str):
    t = unicodedata.normalize("NFKC", str(text))
    m = re.search(r"(20\d{2})\s*年\s*(\d{1,2})\s*月", t)
    if m:
        return f"{m.group(1)}-{int(m.group(2)):02d}"
    m = re.search(r"令和\s*(\d+)\s*年\s*(\d{1,2})\s*月", t)
    if m:
        return f"{2018 + int(m.group(1))}-{int(m.group(2)):02d}"
    m = re.search(r"(20\d{2})[-/]?(0[1-9]|1[0-2])(?!\d)", t)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def estat_find_latest(app_id: str):
    """データカタログから『都市別小売価格』のExcelを探し、最新月の (年月, [(URL, 表題)]) を返す"""
    from urllib.parse import urlencode

    q = urlencode({"appId": app_id, "statsCode": "00200571", "searchWord": "都市別小売価格", "limit": "500"})
    js = json.loads(http_get(f"{ESTAT_CATALOG}?{q}").decode("utf-8"))
    root = js.get("GET_DATA_CATALOG", {})
    res = root.get("RESULT", {})
    if str(res.get("STATUS")) not in ("0", "1", "2"):
        raise ParseError(f"e-Stat API エラー: STATUS={res.get('STATUS')} {res.get('ERROR_MSG')}（アプリケーションIDを確認）")

    cands = []
    for entry in as_list(root.get("DATA_CATALOG_LIST_INF", {}).get("DATA_CATALOG_INF")):
        ds_text = json.dumps(entry.get("DATASET", {}), ensure_ascii=False)
        for r in as_list((entry.get("RESOURCES") or {}).get("RESOURCE")):
            url = r.get("URL") or ""
            r_text = json.dumps(r, ensure_ascii=False)
            fmt = str(r.get("FORMAT", "")).upper()
            if "XLS" not in fmt and not re.search(r"\.xlsx?", url, re.I):
                continue
            if "都市別" not in r_text + ds_text:
                continue
            ym = _ym_from(r.get("SURVEY_DATE", "")) or _ym_from(r_text) or _ym_from(ds_text)
            if ym and url:
                title = r.get("TITLE", {})
                title = title.get("NAME") if isinstance(title, dict) else title
                cands.append((ym, url, str(title)))
    if not cands:
        raise ParseError("e-Stat のデータカタログに『都市別小売価格』のExcelが見つかりません")
    latest = max(c[0] for c in cands)
    files = [(u, t) for ym, u, t in cands if ym == latest]
    print(f"e-Stat 最新: {latest}（候補 {len(cands)}件中 {len(files)}ファイル）")
    for u, t in files:
        print(f"  - {t} {u}")
    return latest, files


def excel_rows(data: bytes):
    """Excel（xlsx / 旧xls どちらでも）→ [(シート名, [[セル, ...], ...]), ...]"""
    out = []
    if data[:2] == b"PK":
        import openpyxl

        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        for ws in wb.worksheets:
            out.append((ws.title, [list(r) for r in ws.iter_rows(values_only=True)]))
    else:
        import xlrd

        wb = xlrd.open_workbook(file_contents=data)
        for sh in wb.sheets():
            out.append((sh.name, [sh.row_values(i) for i in range(sh.nrows)]))
    return out


def _cell(v) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return unicodedata.normalize("NFKC", str(v)).strip()


def _num(v):
    s = _cell(v).replace(",", "")
    try:
        f = float(s)
        return f if f > 0 else None
    except ValueError:
        return None  # 「-」「…」「x」など＝価格なし


def parse_estat_sheet(rows):
    """札幌市の列がある表から [ {code, name, spec, unit, price} ] を取り出す。列の位置は見出しと中身から自動判定"""
    # 1) 「札幌」を含むセルがある行＝見出し行
    hdr_i = city_col = None
    for i, row in enumerate(rows[:60]):
        for j, v in enumerate(row):
            if CITY in _cell(v):
                hdr_i, city_col = i, j
                break
        if hdr_i is not None:
            break
    if hdr_i is None:
        return None
    ncol = max(len(r) for r in rows)
    # 見出しは複数行に分かれていることがあるので、見出し行とその上4行を列ごとに連結
    labels = []
    for j in range(ncol):
        parts = [_cell(rows[k][j]) if j < len(rows[k]) else "" for k in range(max(0, hdr_i - 4), hdr_i + 1)]
        labels.append(" ".join(p for p in parts if p))
    body = [r for r in rows[hdr_i + 1:] if any(_cell(v) for v in r)]

    def col_ratio(j, pred):
        vals = [_cell(r[j]) for r in body if j < len(r) and _cell(r[j])]
        return (sum(1 for v in vals if pred(v)) / len(vals)) if vals else 0

    left = range(0, city_col)  # 品目情報は札幌列より左にある
    code_col = next((j for j in left if col_ratio(j, lambda v: re.fullmatch(r"\d{4,5}", v)) > 0.6), None)
    unit_col = next((j for j in left if "単位" in labels[j]), None)
    if unit_col is None:
        unit_col = next((j for j in left if col_ratio(j, lambda v: UNIT_WORD_RE.match(v)) > 0.5), None)
    spec_col = next((j for j in left if "銘柄" in labels[j]), None)
    name_col = next((j for j in left if "品目" in labels[j] and "符号" not in labels[j] and j not in (code_col, spec_col, unit_col)), None)
    if name_col is None:  # 見出しで分からなければ、符号列の次の文字列の列
        name_col = next((j for j in left if j not in (code_col, spec_col, unit_col)
                         and col_ratio(j, lambda v: not re.fullmatch(r"[\d.,\s]+", v)) > 0.8), None)
    print(f"    見出し行={hdr_i + 1} 札幌列={city_col + 1} 符号列={code_col} 品目列={name_col} 銘柄列={spec_col} 単位列={unit_col}")
    if name_col is None:
        return []

    items = []
    for r in body:
        get = lambda j: _cell(r[j]) if j is not None and j < len(r) else ""
        name, code = get(name_col), get(code_col)
        m = re.match(r"^(\d{4,5})\s*(.+)$", name)  # 「1401 キャベツ」のように符号と名前が同じセルの場合
        if m:
            code, name = code or m.group(1), m.group(2)
        price = _num(r[city_col]) if city_col < len(r) else None
        if not name or not code.isdigit() or price is None:
            continue
        items.append({"code": int(code), "name": name, "spec": get(spec_col), "unit": get(unit_col), "price": price})
    return items


MULTI_RE = re.compile(r"\d+\s*(缶|個|本|袋|枚|パック|箱|ロール|組|切|尾|玉)\s*入|[×xX＊*]\s*\d")


def _amount_in(text: str):
    t = unicodedata.normalize("NFKC", text or "").lower().replace("ℓ", "l").replace("リットル", "l")
    found = UNIT_RE.findall(t)
    if len(found) != 1:  # 0個＝不明、2個以上＝どれが1単位分か曖昧
        return None
    v, u = float(found[0][0]), found[0][1]
    amt = v * (1000 if u in ("kg", "l") else 1)
    return (int(amt) if amt.is_integer() else amt), u in ("ml", "l")


def unit_to_amount(unit: str, spec: str):
    """数量単位 → (g または ml の量, 液体か)。換算できなければ (None, False)
    1) 単位列そのもの（1kg / 100g / 1000ml）を最優先
    2) 単位が「1袋」「1本」などで、銘柄に量が1つだけ書かれていて、入り数（6缶入り等）が無い場合は銘柄の量を使う
       例：うるち米 1袋・「5kg袋入り」→5000g ／ ビール 1パック・「350ml缶入り,6缶入り」→ 曖昧なので換算しない"""
    u = unicodedata.normalize("NFKC", unit or "").strip().lower()
    if u in ("kg", "g"):
        return (1000 if u == "kg" else 1), False
    r = _amount_in(u)
    if r:
        return r
    if re.fullmatch(r"1\s*(袋|本|個|パック|缶|箱|瓶|枚|丁|玉|束)", u) and not MULTI_RE.search(unicodedata.normalize("NFKC", spec or "")):
        r = _amount_in(spec)
        if r:
            return r
    return None, False


def make_readings(names):
    try:
        import pykakasi

        kks = pykakasi.kakasi()
        return {n: "".join(x["hira"] for x in kks.convert(n)) for n in names}
    except Exception as e:  # noqa: BLE001
        print(f"::warning::よみがな生成をスキップ（{e}）。漢字・カタカナでの検索は可能")
        return {}


def build_estat(raw_items, old_items):
    old = {e["c"]: e for e in old_items}
    base_names = {}
    out, skipped_ratio = [], []
    for it in raw_items:
        code, name = it["code"], it["name"]
        food = code in FOOD_CODE
        if not food and not any(k in name for k in NONFOOD_KEYWORDS):
            continue
        base = re.split(r"[(（]", name)[0].strip()
        if base in ESTAT_DUP or name in ESTAT_DUP:
            continue
        g, vol = unit_to_amount(it["unit"], it["spec"])
        price = round(it["price"])
        prev = old.get(code)
        if prev and not (RATIO_MIN <= price / prev["p"] <= RATIO_MAX):
            skipped_ratio.append(f"{name}: {prev['p']}→{price}")
            price = prev["p"]  # 極端な変化は読み取りミスとみなして前回値を維持
        e = {"c": code, "n": name, "s": it["spec"], "u": it["unit"], "g": g, "p": price}
        if vol:
            e["v"] = 1
        base_names[code] = base
        out.append(e)
    # 同じ品目符号が複数シートに出てくる場合は最初の1つ
    seen, uniq = set(), []
    for e in out:
        if e["c"] not in seen:
            seen.add(e["c"])
            uniq.append(e)
    readings = make_readings([e["n"] for e in uniq])
    for e in uniq:
        k = [readings.get(e["n"], "")]
        k += [v for key, v in ESTAT_ALIAS.items() if key in e["n"]]
        k = " ".join(x for x in k if x)
        if k:
            e["k"] = k
    if skipped_ratio:
        print("::warning::前回から極端に変わった品目は前回値を維持: " + "、".join(skipped_ratio[:20]))
    return sorted(uniq, key=lambda e: e["c"])


def estat_update(app_id: str, force: bool) -> bool:
    html = INDEX.read_text(encoding="utf-8")
    m = ESTAT_RE.search(html)
    if not m:
        raise ParseError("index.html に /*ESTAT-BEGIN*/ 〜 /*ESTAT-END*/ が見つかりません")
    cur = json.loads(m.group(1))
    latest, files = estat_find_latest(app_id)
    if cur.get("period") and latest <= cur["period"] and not force:
        print(f"e-Stat：新しいデータはありません（取り込み済み {cur['period']}）")
        return False

    raw = []
    for url, title in files:
        for sheet, rows in excel_rows(http_get(url)):
            got = parse_estat_sheet(rows)
            if got is None:
                continue
            print(f"  シート「{sheet}」: 札幌市の価格 {len(got)}件")
            raw += got
    items = build_estat(raw, cur.get("items", []))
    if len(items) < ESTAT_MIN_ITEMS:
        sample = "\n  ".join(str(x) for x in raw[:10])
        raise ParseError(f"e-Stat：読み取れた品目が {len(items)}件しかありません（表の形が想定外）。先頭の例:\n  {sample}")
    no_g = sum(1 for e in items if not e["g"])
    print(f"e-Stat：{len(items)}品目を取り込み（うち重さ換算できない物 {no_g}件は1単位で比較）")
    for e in items[:8]:
        print(f"  {e}")
    new = {"period": latest, "file": files[0][0], "items": items}
    if new == cur:
        print("e-Stat：内容に変化なし")
        return False
    body = json.dumps(new, ensure_ascii=False, separators=(",", ":"))
    body = body.replace('"items":[', '"items":[\n').replace("},{", "},\n{")
    INDEX.write_text(ESTAT_RE.sub(lambda _: "/*ESTAT-BEGIN*/" + body + "/*ESTAT-END*/", html, count=1),
                     encoding="utf-8", newline="\n")
    return True


def main() -> int:
    import os

    ap = argparse.ArgumentParser()
    ap.add_argument("--verify", action="store_true", help="取り込み済みでも最新を解析し直す（手動実行・push時）")
    ap.add_argument("--no-fetch", action="store_true", help="通信せず sw.js の版数だけ更新")
    args = ap.parse_args()

    failed = False
    if not args.no_fetch:
        # 札幌市（定番16品目）
        try:
            fetch_and_update(args.verify)
        except NetError as e:
            print(f"::warning::{e}（今回はスキップ）")
        except ParseError as e:
            print(f"::error::札幌市データ: {e}")
            failed = True
        # 総務省（検索用の品目）。札幌市側が失敗しても実行する
        app_id = os.environ.get("ESTAT_APP_ID", "").strip()
        if not app_id:
            print("::notice::ESTAT_APP_ID が未設定なので総務省データの取り込みはスキップ（定番16品目のみ）")
        else:
            try:
                estat_update(app_id, args.verify)
            except NetError as e:
                print(f"::warning::{e}（今回はスキップ）")
            except ParseError as e:
                print(f"::error::総務省データ: {e}")
                failed = True
    bump_version()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
