"""
🏆 DMM同人ランキング → X本投稿ツール（Buffer 新API・自動公開版）
================================================================
DMM Webサービス（アフィリエイト）APIの ItemList エンドポイントから
「同人」カテゴリーの人気ランキングを取得し、上位3件（既出作品は除外して繰り下げ）分の
X投稿用スレッドをコンソールに出力し、Buffer連携が有効な場合はBufferのキューに
登録して自動公開します。

⚠️ 重要：BUFFER_API_KEY / BUFFER_CHANNEL_IDS を設定すると、生成したスレッドは
   人間の承認を挟まずBufferのキューに登録され、Buffer側の投稿スケジュールに従って
   自動的にXへ公開されます（saveToDraft=false）。公開前に人間が内容を確認する
   ステップは存在しないため、実行前に投稿内容・アカウント設定を十分確認してください。

   ※ Xの自動化ポリシー・成人向けコンテンツポリシーに抵触するリスクがあるため、
      本番運用する場合はアカウント側の設定（センシティブコンテンツ表示設定等）や
      Xの利用規約を事前にご確認ください。

   ※ 旧Buffer Publish API（api.bufferapp.com/1/...）は2025年1月3日をもって
      廃止されており、現在は新しいBuffer API（GraphQL, https://api.buffer.com,
      Bearer認証, 個人APIキーは https://publish.buffer.com/settings/api で発行）
      に統一されています。本スクリプトはこの新API向けに実装されています。

   ※ 新Buffer APIはXの「返信スレッド」をネイティブにサポートしています
      （createPost の metadata.twitter.thread フィールド）。そのため本スクリプトは
      1作品＝1回の createPost 呼び出しで、5投稿を正式なスレッドとして登録します。
      ※ Buffer APIは現状ベータ版のため、仕様が変更される可能性があります。
        実行前に https://developers.buffer.com/ で最新仕様をご確認ください。

■ 投稿スレッド構成（1作品あたり2投稿＝1スレッド）【v2: Xのアルゴリズム変更・シャドウバン対策】
  投稿1（メイン）   : フック1行（レビュー等の事実）+ 作品名 + サークル名 + #PR
                      ＋（成人向けメディアの上限に余裕がある場合のみ）サンプル画像を最大4枚まで1投稿に添付
  リプライ2         : 冒頭文（ローテーション）+ 価格 + アフィリエイトURL + #PR

■ v2の変更点（旧: 5投稿すべてに成人向けサンプル画像、(1/5)〜(5/5)の連投スレッド、Buffer任せの投稿時刻）
  1. 成人向け画像は「直近5スレッド中 ADULT_MEDIA_MAX_IN_LAST5 件まで」（既定1）。
     直近5投稿中3件で警告→フォロワー外に一切表示されなくなるため。以前は1日15投稿すべて画像付きだった。
  2. 画像を付ける場合も、5投稿に分けず「1投稿に最大4枚」にまとめる（メディア付き投稿は1件扱い）。
  3. 「(2/5)」のような中身のない連投リプライを廃止（質の低い投稿・スパム判定の回避）。
  4. 投稿時刻をBuffer任せにせず、昼12-13時・夜20-22時の枠に1スレッドずつ予約（連投回避）。
  5. 未成年を想起させる作品（タイトル・ジャンルに該当語を含むもの）を投稿対象から除外。
  6. Buffer送信に失敗した作品は履歴に残さず、次回再挑戦する。
  ※ アフィリエイトURLへは一切アクセスしない（クリック数の水増しなし）。

■ 必須環境変数
  DMM_API_ID        : DMM Webサービスの API ID
  DMM_AFFILIATE_ID  : DMMアフィリエイトID（例: xxxxx-990）

■ 任意環境変数
  DMM_FLOOR         : 検索対象フロア（デフォルト 'doujin'）
  RANK_COUNT        : 1日に何件（何位まで）投稿するか（デフォルト 3）
  FETCH_HITS        : APIから一度に取得する件数（デフォルト 30。重複除外後に5件残らない場合は増やす）
  HISTORY_KEEP_DAYS : 重複判定のため投稿済み履歴を保持する日数（デフォルト 30）
  OUTPUT_DIR        : 下書きファイルの出力先（デフォルト ./outputs）

■ 任意環境変数（Buffer連携。両方設定した場合のみ有効）
  BUFFER_API_KEY      : Buffer個人APIキー（https://publish.buffer.com/settings/api で発行）
  BUFFER_CHANNEL_IDS  : 送信先BufferチャンネルID（Xチャンネル）をカンマ区切りで指定
                        （チャンネルIDは GraphQL の `channels` クエリで取得可能。
                          未設定の場合はBuffer連携をスキップし、従来どおりコンソール出力のみ行う）
"""

import os
import sys
import json
import datetime
import random
import re
import requests
from pathlib import Path

# ================================================================
# ⚙️ 設定
# ================================================================

DMM_API_ID = os.environ.get('DMM_API_ID', '').strip()
DMM_AFFILIATE_ID = os.environ.get('DMM_AFFILIATE_ID', '').strip()
DMM_FLOOR = os.environ.get('DMM_FLOOR', 'doujin').strip()
RANK_COUNT = int(os.environ.get('RANK_COUNT', '2'))  # v2: 3→2（連投で評価を食い合わないため）
FETCH_HITS = int(os.environ.get('FETCH_HITS', '30'))
HISTORY_KEEP_DAYS = int(os.environ.get('HISTORY_KEEP_DAYS', '30'))

# --- Buffer連携（任意・新API/GraphQL版）-----------------------------------
BUFFER_API_KEY = os.environ.get('BUFFER_API_KEY', '').strip()
BUFFER_CHANNEL_IDS = [
    cid.strip() for cid in os.environ.get('BUFFER_CHANNEL_IDS', '').split(',') if cid.strip()
]
BUFFER_ENABLED = bool(BUFFER_API_KEY and BUFFER_CHANNEL_IDS)
BUFFER_GRAPHQL_ENDPOINT = 'https://api.buffer.com'
# False: 下書き保存ではなく、Bufferのキューに正式なポストとして追加する。
# schedulingType='automatic' のため、Buffer側の投稿スケジュール（プランごとの
# 投稿時間枠）に従って自動的に公開される。人間の承認ステップは挟まらない。
BUFFER_SAVE_AS_DRAFT = False

# --- 予約方式（v2） ---
#   customScheduled: dueAtで指定した時刻に予約（既定。枠ごとに1スレッドだけ予約して連投を避ける）
#   addToQueue     : Buffer側のスケジュール枠に任せる（投稿時刻が固まりやすいので非推奨）
BUFFER_SCHEDULING_MODE = os.environ.get('BUFFER_SCHEDULING_MODE', 'customScheduled').strip()
BUFFER_INITIAL_DELAY_MIN = float(os.environ.get('BUFFER_INITIAL_DELAY_MIN', '10'))
BUFFER_TIMEZONE_OFFSET_HOURS = float(os.environ.get('BUFFER_TIMEZONE_OFFSET_HOURS', '9'))  # JST
_DEFAULT_WINDOWS = [(12, 13), (20, 22)]
try:
    _w = os.environ.get('POST_WINDOWS', '').strip()
    POST_WINDOWS = [tuple(x) for x in json.loads(_w)] if _w else _DEFAULT_WINDOWS
except Exception:
    print('⚠️  POST_WINDOWSの形式が不正なため、既定の時間帯（12-13時・20-22時）を使用します。')
    POST_WINDOWS = _DEFAULT_WINDOWS

# --- 成人向けメディアの上限（Xの新ルール：直近5投稿中3件で警告→フォロワー外に非表示） ---
ADULT_MEDIA_MAX_IN_LAST5 = int(os.environ.get('ADULT_MEDIA_MAX_IN_LAST5', '1'))
ADULT_MEDIA_LOOKBACK = int(os.environ.get('ADULT_MEDIA_LOOKBACK', '5'))

# --- 未成年を想起させる作品の除外（アカウント停止リスクの回避。完全な検出ではない） ---
EXCLUDE_MINOR_THEMED = os.environ.get('EXCLUDE_MINOR_THEMED', 'true').strip().lower() == 'true'
MINOR_THEMED_WORDS = [
    'ロリ', 'ショタ', '幼女', '幼児', '幼い', '少女', '女児', '男児', '児童', '園児', 'ランドセル', '未成年',
    '小学', '中学', '高校', '女子校', '男子校', '女子高', '男子高', '学生', '生徒', '制服', '学園', '学校',
    '◯学', '○学', '〇学', '◯徒', '○徒', '〇徒', '◯校', '○校', '〇校', 'JK', 'JC', 'JS', 'ＪＫ', 'ＪＣ', 'ＪＳ',
]
MINOR_THEMED_WORDS += [w.strip() for w in os.environ.get('EXTRA_EXCLUDE_WORDS', '').split(',') if w.strip()]

DMM_API_ENDPOINT = 'https://api.dmm.com/affiliate/v3/ItemList'
DMM_FLOORLIST_ENDPOINT = 'https://api.dmm.com/affiliate/v3/FloorList'
IMAGES_IN_MAIN = int(os.environ.get('IMAGES_IN_MAIN', '4'))  # メイン投稿1件に添付するサンプル画像の最大枚数（Xの上限は4枚）
IMAGES_PER_ITEM = IMAGES_IN_MAIN

if not DMM_API_ID or not DMM_AFFILIATE_ID:
    print('❌ DMM_API_ID / DMM_AFFILIATE_ID が未設定です。環境変数を設定してください。')
    sys.exit(1)


def get_output_dir():
    base = os.environ.get('OUTPUT_DIR', '').strip()
    p = Path(base) if base else Path(__file__).resolve().parent / 'outputs'
    p.mkdir(parents=True, exist_ok=True)
    return p


HISTORY_FILE = get_output_dir() / 'dmm_doujin_ranking_history.json'


# ================================================================
# 📁 履歴管理（重複除外用：content_id ベース）
# ================================================================

def load_history():
    if not HISTORY_FILE.exists():
        return []
    try:
        raw = json.loads(HISTORY_FILE.read_text(encoding='utf-8'))
    except Exception:
        return []
    cutoff = (datetime.date.today() - datetime.timedelta(days=HISTORY_KEEP_DAYS)).isoformat()
    return [h for h in raw if h.get('date', '') >= cutoff]


def save_history(history):
    HISTORY_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=2), encoding='utf-8')


# ================================================================
# 🛡️ v2: 投稿ログ・成人向けメディア上限・予約枠・文字数カウント
# ================================================================

POST_LOG = []  # [{'content_id','due_at'(UTC ISO),'adult_media'(bool)}] 直近の投稿記録（1スレッド=1件）
POST_LOG_FILE = get_output_dir() / 'dmm_doujin_post_log.json'


def load_post_log():
    try:
        if POST_LOG_FILE.exists():
            data = json.loads(POST_LOG_FILE.read_text(encoding='utf-8'))
            if isinstance(data, list):
                return data
    except Exception as e:
        print(f'⚠️  投稿ログの読み込みに失敗しました: {e}')
    return []


def save_post_log():
    try:
        del POST_LOG[:-60]
        POST_LOG_FILE.write_text(json.dumps(POST_LOG, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception as e:
        print(f'⚠️  投稿ログの保存に失敗しました: {e}')


def adult_media_allowed():
    """今回のスレッドに成人向け画像を付けてよいか。
    今回分を含めた直近ADULT_MEDIA_LOOKBACK件のうち、画像付きがADULT_MEDIA_MAX_IN_LAST5件以内に収まるならTrue。"""
    if ADULT_MEDIA_MAX_IN_LAST5 <= 0:
        return False
    entries = sorted(POST_LOG, key=lambda e: e.get('due_at', ''))
    prev = entries[-(ADULT_MEDIA_LOOKBACK - 1):] if ADULT_MEDIA_LOOKBACK > 1 else []
    return sum(1 for e in prev if e.get('adult_media')) + 1 <= ADULT_MEDIA_MAX_IN_LAST5


def _parse_utc(sv):
    try:
        return datetime.datetime.strptime(str(sv)[:19], '%Y-%m-%dT%H:%M:%S')
    except Exception:
        return None


def _window_key(local_dt):
    for idx, (s_h, e_h) in enumerate(POST_WINDOWS):
        ws = datetime.datetime.combine(local_dt.date(), datetime.time(hour=int(s_h)))
        we = datetime.datetime.combine(local_dt.date(), datetime.time(hour=int(e_h)))
        if ws <= local_dt <= we:
            return (local_dt.date(), idx)
    return None


def _occupied_windows():
    occ = set()
    for e in POST_LOG:
        due = _parse_utc(e.get('due_at', ''))
        if due is not None:
            k = _window_key(due + datetime.timedelta(hours=BUFFER_TIMEZONE_OFFSET_HOURS))
            if k:
                occ.add(k)
    return occ


def compute_due_at():
    """1つの時間帯枠につき1スレッドだけになるよう、予約時刻（UTCのdatetime）を返す。
    実行時点で過去になった枠・すでに予約済みの枠は飛ばし、枠内の時刻は秒単位でランダムにする。"""
    local_now = datetime.datetime.utcnow() + datetime.timedelta(hours=BUFFER_TIMEZONE_OFFSET_HOURS)
    earliest = local_now + datetime.timedelta(minutes=BUFFER_INITIAL_DELAY_MIN)
    occupied = _occupied_windows()
    order = sorted(enumerate(POST_WINDOWS), key=lambda iw: iw[1][0])
    for day_offset in range(0, 15):
        day = local_now.date() + datetime.timedelta(days=day_offset)
        for idx, (s_h, e_h) in order:
            if (day, idx) in occupied:
                continue
            ws = datetime.datetime.combine(day, datetime.time(hour=int(s_h)))
            we = datetime.datetime.combine(day, datetime.time(hour=int(e_h)))
            lo = max(ws, earliest)
            if (we - lo).total_seconds() < 600:
                continue
            local_due = lo + datetime.timedelta(seconds=random.uniform(0, (we - lo).total_seconds()))
            return local_due - datetime.timedelta(hours=BUFFER_TIMEZONE_OFFSET_HOURS)
    return datetime.datetime.utcnow() + datetime.timedelta(minutes=BUFFER_INITIAL_DELAY_MIN)


# Xの重み付き文字数（半角=1・日本語/絵文字=2・URL=23）で280以内に収めるための簡易カウンタ
_X_LOW_RANGES = [(0, 4351), (8192, 8205), (8208, 8223), (8242, 8247)]
_X_URL_RE = re.compile(r'https?://\S+')


def x_text_length(text):
    urls = _X_URL_RE.findall(text)
    rest = _X_URL_RE.sub('', text)
    w = sum(1 if any(lo <= ord(c) <= hi for lo, hi in _X_LOW_RANGES) else 2 for c in rest)
    return w + 23 * len(urls)


def is_minor_themed(item):
    """タイトル・ジャンル名に未成年を想起させる語を含むか（EXCLUDE_MINOR_THEMED=true時のみ使用）。"""
    info = item.get('iteminfo') or {}
    genres = ' '.join(g.get('name', '') for g in (info.get('genre') or []))
    hay = f"{item.get('title', '')} {genres}"
    hay_up = hay.upper()
    return next((w for w in MINOR_THEMED_WORDS if w.upper() in hay_up), None)


# ================================================================
# 🌐 DMM API 呼び出し
# ================================================================

def resolve_floor_code():
    """FloorList APIから site=FANZA / service(またはfloor)=DMM_FLOOR に対応する
    正式な service名 と floorコードを取得する。見つからない場合は候補一覧を表示して終了する。"""
    params = {
        'api_id': DMM_API_ID,
        'affiliate_id': DMM_AFFILIATE_ID,
        'output': 'json',
    }
    try:
        resp = requests.get(DMM_FLOORLIST_ENDPOINT, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f'❌ FloorList APIへのリクエストに失敗しました: {e}')
        sys.exit(1)

    sites = data.get('result', {}).get('site', [])
    candidates = []  # (site_code, service_code, floor_id, floor_code, floor_name, site_name, service_name)

    for site in sites:
        site_code = site.get('code', site.get('name', ''))
        site_name = site.get('name', '')
        for service in site.get('service', []):
            service_code = service.get('code', service.get('name', ''))
            service_name = service.get('name', '')
            for floor in service.get('floor', []):
                candidates.append((
                    site_code,
                    service_code,
                    floor.get('id'),
                    floor.get('code'),
                    floor.get('name'),
                    site_name,
                    service_name,
                ))

    # 1) service名(表示名)またはservice_code が DMM_FLOOR と完全一致するもの
    for site_code, service_code, floor_id, floor_code, floor_name, site_name, service_name in candidates:
        if service_code == DMM_FLOOR or service_name == DMM_FLOOR:
            return site_code, service_code, floor_code

    # 2) floorコード自体がDMM_FLOORと一致するもの
    for site_code, service_code, floor_id, floor_code, floor_name, site_name, service_name in candidates:
        if floor_code == DMM_FLOOR:
            return site_code, service_code, floor_code

    # 3) service名/floor名にDMM_FLOORが部分一致するもの
    for site_code, service_code, floor_id, floor_code, floor_name, site_name, service_name in candidates:
        haystack = f'{service_code} {service_name} {floor_code} {floor_name}'.lower()
        if DMM_FLOOR.lower() in haystack:
            return site_code, service_code, floor_code

    print(f'❌ service/floor = "{DMM_FLOOR}" が見つかりませんでした。')
    all_site_names = sorted(set(f'{c[5]}({c[0]})' for c in candidates))
    print(f'   FloorListから取得できた site 一覧: {all_site_names}')
    print('   取得できた site/service/floor の一覧（全件）:')
    for site_code, service_code, floor_id, floor_code, floor_name, site_name, service_name in candidates:
        print(f'   - site={site_code}({site_name})  service={service_code}({service_name})  floor={floor_code}({floor_name})')
    sys.exit(1)


def fetch_ranking(hits=FETCH_HITS):
    """DMM ItemList APIから人気順（rank）の同人ランキングを取得する。"""
    site_code, service_code, floor_code = resolve_floor_code()
    print(f'ℹ️ 使用する site={site_code} / service={service_code} / floor={floor_code}')

    params = {
        'api_id': DMM_API_ID,
        'affiliate_id': DMM_AFFILIATE_ID,
        'site': site_code,
        'service': service_code,
        'floor': floor_code,
        'hits': hits,
        'sort': 'rank',
        'output': 'json',
    }
    resp = requests.get(DMM_API_ENDPOINT, params=params, timeout=30)
    if resp.status_code != 200:
        print(f'❌ DMM APIへのリクエストに失敗しました: {resp.status_code} {resp.reason}')
        print(f'   リクエストURL: {resp.url}')
        print(f'   レスポンス本文: {resp.text[:1000]}')
        sys.exit(1)
    try:
        data = resp.json()
    except Exception as e:
        print(f'❌ レスポンスのJSON解析に失敗しました: {e}')
        print(f'   レスポンス本文: {resp.text[:1000]}')
        sys.exit(1)

    result = data.get('result', {})
    if result.get('status') and str(result.get('status')) != '200':
        print(f'❌ DMM APIエラー: {result}')
        sys.exit(1)

    return result.get('items', [])


def extract_sample_images(item, max_images=IMAGES_PER_ITEM):
    """商品データからサンプル画像URLを最大 max_images 枚取り出す。"""
    images = []
    sample = (item.get('sampleImageURL') or {})
    # DMM APIは 'sample_s'（小）や 'sample_l'（大）にimage配列を持つ
    for key in ('sample_l', 'sample_s'):
        block = sample.get(key)
        if block and block.get('image'):
            images = block['image']
            break
    if not images:
        # フォールバック：パッケージ画像だけでも入れておく
        pkg = (item.get('imageURL') or {}).get('large') or (item.get('imageURL') or {}).get('list')
        if pkg:
            images = [pkg]
    return images[:max_images]


def build_ranked_list_excluding_duplicates(items, used_ids, rank_count):
    """既出（used_idsに含まれる）content_idをスキップしながら、
    ランキング順に rank_count 件選び出す（自動で順位が繰り下がる）。"""
    picked = []
    display_rank = 0
    for item in items:
        display_rank += 1
        content_id = item.get('content_id') or item.get('product_id')
        if not content_id:
            continue
        if content_id in used_ids:
            continue
        picked.append((display_rank, item))
        if len(picked) >= rank_count:
            break
    return picked


# ================================================================
# 📝 投稿スレッド下書きの組み立て
# ================================================================

REPLY_LEADS = [  # 同じ文章の使い回しはスパム扱いされやすいためローテーションする
    '▼ 詳細・サンプルはこちら',
    '▼ 作品ページはこちら',
    '▼ サンプルが見られます',
    '▼ 気になった方はこちら',
    '▼ 購入前にサンプル確認できます',
    '▼ 詳しくはこちらから',
]
PR_TAG = '#PR'


def _item_facts(item):
    review = item.get('review') or {}
    info = item.get('iteminfo') or {}
    try:
        cnt = int(review.get('count') or 0)
    except (TypeError, ValueError):
        cnt = 0
    try:
        avg = float(review.get('average')) if review.get('average') not in (None, '') else None
    except (TypeError, ValueError):
        avg = None
    makers = [m.get('name') for m in (info.get('maker') or []) if m.get('name')]
    price = str((item.get('prices') or {}).get('price') or '').strip()
    return {'avg': avg, 'cnt': cnt, 'maker': makers[0] if makers else '', 'price': price,
            'date': str(item.get('date') or '')}


def _is_recent(date_str, days=3):
    try:
        d = datetime.datetime.strptime(date_str[:10], '%Y-%m-%d').date()
    except Exception:
        return False
    return 0 <= (datetime.date.today() - d).days <= days


def _build_hook(f):
    """フック1行。DMM APIの実データ（レビュー・配信日・サークル名）だけから作る。"""
    cands = []
    if f['avg'] and f['cnt'] >= 10 and f['avg'] >= 4.0:
        cands += [f"レビュー{f['cnt']}件・平均{f['avg']}の高評価作品", f"平均{f['avg']}（レビュー{f['cnt']}件）と評価が高い一本"]
    elif f['avg'] and f['cnt']:
        cands.append(f"レビュー平均{f['avg']}（{f['cnt']}件）")
    if _is_recent(f['date']):
        cands += ['配信開始したばかりの新作', '【新着】最近配信が始まった作品']
    if f['maker']:
        cands.append(f"{f['maker'][:20]}の作品")
    cands.append('FANZA同人で人気の作品')  # sort=rank（人気順）で取得しているため事実と矛盾しない
    return random.choice(cands)


def build_thread_draft(rank, item, with_media=False):
    """2投稿スレッド（メイン＋リンクのリプライ）を組み立てる。with_media=Trueのときだけ
    メイン投稿にサンプル画像を最大IMAGES_IN_MAIN枚添付する（成人向けメディア上限の判定は呼び出し側）。"""
    title = item.get('title', '(タイトル不明)')
    affiliate_url = item.get('affiliateURL') or item.get('URL', '')
    f = _item_facts(item)
    hook = _build_hook(f)

    def main_text(title_limit, use_hook=True):
        t = (title[:title_limit] + '…') if len(title) > title_limit else title
        lines = [hook if use_hook else None, f'📖 {t}', f"✍ サークル：{f['maker'][:25]}" if f['maker'] else None]
        return '\n'.join(l for l in lines if l) + '\n\n' + PR_TAG

    limit, use_hook = 40, True
    text = main_text(limit, use_hook)
    while x_text_length(text) > 280 and limit > 8:
        limit -= 4
        text = main_text(limit, use_hook)
    if x_text_length(text) > 280:
        text = main_text(limit, False)

    reply_lines = [random.choice(REPLY_LEADS)]
    if f['price']:
        reply_lines.append(f"🪙 {f['price']}円" if f['price'].isdigit() else f"🪙 {f['price']}")
    reply_lines += [affiliate_url, PR_TAG]

    images = extract_sample_images(item, max_images=IMAGES_IN_MAIN) if with_media else []
    return [
        {'label': '投稿1（メイン）', 'text': text, 'images': images},
        {'label': 'リプライ2（リンク）', 'text': '\n'.join(reply_lines), 'images': []},
    ]


def print_thread_draft(rank, item, posts):
    content_id = item.get('content_id') or item.get('product_id') or '(ID不明)'
    print('\n' + '-' * 60)
    print(f'🏅 {rank}位: {item.get("title", "(タイトル不明)")}  [content_id: {content_id}]')
    print('-' * 60)
    for p in posts:
        print(f'\n  ▶ {p["label"]}')
        print('    本文: ' + p['text'].replace('\n', '\n          '))
        print(f'    画像: {len(p["images"])}枚' if p['images'] else '    画像: なし')


# ================================================================
# 🧵 Buffer連携（新API / GraphQL）
#    saveToDraft=false でBufferキューに登録し、自動公開する。
# ================================================================

def buffer_graphql_request(query, variables=None):
    """Buffer GraphQL APIへリクエストを送る共通関数。

    GraphQLの流儀上、HTTPレベルのエラー（認証エラー等）以外は常に200が返り、
    エラー情報は response['errors'] や MutationError として返ってくる点に注意。
    """
    headers = {
        'Content-Type': 'application/json',
        'Authorization': f'Bearer {BUFFER_API_KEY}',
    }
    payload = {'query': query, 'variables': variables or {}}
    try:
        resp = requests.post(BUFFER_GRAPHQL_ENDPOINT, headers=headers, json=payload, timeout=30)
    except Exception as e:
        return False, f'Buffer APIへの接続に失敗しました: {e}'

    if resp.status_code != 200:
        return False, f'Buffer APIがHTTP {resp.status_code} を返しました: {resp.text[:500]}'

    try:
        data = resp.json()
    except Exception as e:
        return False, f'Buffer APIレスポンスのJSON解析に失敗しました: {e}'

    if 'errors' in data and data['errors']:
        # システムレベルのGraphQLエラー（認証エラー・レート制限等）
        codes = [err.get('extensions', {}).get('code') for err in data['errors']]
        messages = [err.get('message', '') for err in data['errors']]
        return False, f'Buffer APIエラー（{codes}）: {messages}'

    return True, data.get('data', {})


CREATE_THREADED_DRAFT_POST_MUTATION = """
mutation CreateThreadedDraftPost($input: CreatePostInput!) {
  createPost(input: $input) {
    ... on PostActionSuccess {
      post {
        id
        status
      }
    }
    ... on MutationError {
      message
    }
  }
}
"""


def _asset_input_for_image(image_url):
    """新Buffer API（[AssetInput!]形式）向けに画像1枚をassets要素に変換する。
    プレースホルダー（実URLでない）場合はNoneを返し、呼び出し側で除外する。
    """
    if image_url and image_url.startswith('http'):
        return {'image': {'url': image_url}}
    return None


def build_thread_metadata(posts):
    """postsから Buffer new API の metadata.twitter.thread 用の配列を組み立てる（1投稿に複数画像可）。"""
    thread_items = []
    for post in posts:
        assets = [{'image': {'url': u}} for u in (post.get('images') or []) if u and u.startswith('http')]
        thread_items.append({'text': post['text'], 'assets': assets})
    return thread_items


def send_thread_to_buffer(rank, posts, channel_id, due_at=None):
    """スレッド1本分を1回のcreatePostでBufferに登録する。
    due_at（UTCのdatetime）を渡すとcustomScheduledでその時刻に予約、Noneならaddと同様にキューへ追加。"""
    thread_items = build_thread_metadata(posts)
    if not thread_items:
        return False, 'スレッド項目の組み立てに失敗しました。'

    inp = {
        'text': thread_items[0]['text'],
        'channelId': channel_id,
        'schedulingType': 'automatic',
        'saveToDraft': BUFFER_SAVE_AS_DRAFT,
        'assets': thread_items[0]['assets'],
        'metadata': {'twitter': {'thread': thread_items}},
    }
    if BUFFER_SCHEDULING_MODE == 'addToQueue' or due_at is None:
        inp['mode'] = 'addToQueue'
    else:
        inp['mode'] = 'customScheduled'
        inp['dueAt'] = due_at.strftime('%Y-%m-%dT%H:%M:%S.000Z')

    n_img = sum(len(p.get('images') or []) for p in posts)
    when = f"予約 {inp['dueAt']}" if 'dueAt' in inp else 'キュー追加'
    print(f'\n📤 {rank}位（{len(thread_items)}投稿スレッド・画像{n_img}枚・{when}）をBufferに送信中...')
    ok, result = buffer_graphql_request(CREATE_THREADED_DRAFT_POST_MUTATION, {'input': inp})
    if not ok:
        print(f'   ❌ 送信失敗: {result}')
        return False, result

    create_result = result.get('createPost', {})
    if 'message' in create_result:
        print(f'   ❌ Buffer側でエラー: {create_result["message"]}')
        return False, create_result['message']

    post = create_result.get('post', {})
    print(f'   ✅ Bufferに登録成功: post_id={post.get("id")} status={post.get("status")}')
    return True, post


# ================================================================
# 🚀 メイン処理
# ================================================================

def main():
    history = load_history()
    used_ids = {h['content_id'] for h in history}
    POST_LOG[:] = load_post_log()

    print('=' * 60)
    print(f'📋 DMM同人ランキング投稿生成（対象: 上位{RANK_COUNT}件・重複除外）')
    print('=' * 60)
    if BUFFER_ENABLED:
        print(f'🧵 Buffer連携: 有効（送信先 {len(BUFFER_CHANNEL_IDS)} チャンネル・予約方式 {BUFFER_SCHEDULING_MODE}・saveToDraft={BUFFER_SAVE_AS_DRAFT}）')
        print('   投稿枠（JST）: ' + ', '.join(f'{s}-{e}時' for s, e in POST_WINDOWS) + '（1枠につき1スレッド）')
    else:
        print('🧵 Buffer連携: 無効（BUFFER_API_KEY / BUFFER_CHANNEL_IDS 未設定。コンソール出力のみ）')
    print(f'🛡️ 成人向け画像の上限: 直近{ADULT_MEDIA_LOOKBACK}スレッド中{ADULT_MEDIA_MAX_IN_LAST5}件まで（記録 {len(POST_LOG)}件）')

    items = fetch_ranking(hits=FETCH_HITS)
    if not items:
        print('❌ ランキングデータを取得できませんでした。')
        sys.exit(1)

    if EXCLUDE_MINOR_THEMED:
        kept = []
        for it in items:
            w = is_minor_themed(it)
            if w:
                print(f"   ⏭ 未成年を想起させる語（{w}）を含むためスキップ: {it.get('title', '')[:30]}")
            else:
                kept.append(it)
        items = kept

    picked = build_ranked_list_excluding_duplicates(items, used_ids, RANK_COUNT)
    if len(picked) < RANK_COUNT:
        print(f'⚠️ 重複・除外後、{len(picked)}件しか選べませんでした（FETCH_HITS={FETCH_HITS} を増やすと改善する場合があります）。')

    new_history_entries = []
    sent_count = 0

    for display_rank, item in picked:
        content_id = item.get('content_id') or item.get('product_id')
        media_ok = adult_media_allowed()
        if not media_ok:
            print('🛡️ 成人向け画像の上限に達しているため、今回のスレッドは画像なし（テキストのみ）で投稿します。')
        posts = build_thread_draft(display_rank, item, with_media=media_ok)
        print_thread_draft(display_rank, item, posts)

        sent = False
        due_at = None
        media_used = bool(posts[0]['images'])
        if BUFFER_ENABLED:
            due_at = compute_due_at() if BUFFER_SCHEDULING_MODE != 'addToQueue' else None
            for channel_id in BUFFER_CHANNEL_IDS:
                ok, _ = send_thread_to_buffer(display_rank, posts, channel_id, due_at)
                if not ok and media_used:
                    # 画像が原因で拒否された可能性があるため、テキストのみで1回だけ再試行する
                    print('   ↩️ 画像なしで再試行します。')
                    text_only = build_thread_draft(display_rank, item, with_media=False)
                    ok, _ = send_thread_to_buffer(display_rank, text_only, channel_id, due_at)
                    if ok:
                        media_used = False
                sent = sent or ok

            if not sent:
                # 送信に失敗した作品は履歴に残さず、次回の実行で再挑戦する
                print('   ⚠️ Bufferへの送信に失敗したため、この作品は履歴に記録しません（次回再挑戦）。')
                continue

            sent_count += 1
            POST_LOG.append({
                'content_id': content_id,
                'due_at': (due_at or datetime.datetime.utcnow()).strftime('%Y-%m-%dT%H:%M:%S.000Z'),
                'adult_media': media_used,
            })
            save_post_log()

        new_history_entries.append({
            'content_id': content_id,
            'title': item.get('title', ''),
            'date': datetime.date.today().isoformat(),
        })

    history.extend(new_history_entries)
    save_history(history)

    print('\n' + '=' * 60)
    if BUFFER_ENABLED:
        print(f'✅ 完了: {sent_count}/{len(picked)}件をBufferに予約しました（枠ごとに1スレッド）。')
    else:
        print(f'✅ 完了: {len(picked)}件の投稿内容をコンソールに出力しました。内容を確認のうえ、手動でXに投稿してください。')
    print(f'📚 履歴ファイル（重複判定用）: {HISTORY_FILE}')
    print(f'🛡️ 投稿ログ（画像上限・枠管理用）: {POST_LOG_FILE}')
    print('=' * 60)


if __name__ == '__main__':
    main()
