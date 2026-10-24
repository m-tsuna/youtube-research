"""
YouTube 発信リサーチ（課題8：連携した API に機能を追加）

YouTube Data API を使い、入力したキーワードで「最近6か月以内」に投稿された動画を探して、
本当の再生回数の多い順に表示する。ショート動画だけに絞ることもできる。
ショート動画は「3分以内」で候補を選び、さらに YouTube の /shorts/ のページが開けるかで本物か確かめる。
チャンネル登録者数から「バズ度」（再生回数 ÷ 登録者数）を計算し、
小さいチャンネルでも内容で伸びた動画を見つけやすくする。結果は CSV に保存できる。

使い方：ターミナルで  python main.py  を実行する。
"""

import csv
import html
import os
import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import httplib2
from dotenv import load_dotenv
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

# ============================================================
# 設定（ここをまとめて書いておく）
# ============================================================

# このプログラム（main.py）が置いてあるフォルダ
BASE_DIR = Path(__file__).resolve().parent

# API キーを書いておくファイルと、その中の項目名
ENV_FILE = BASE_DIR / ".env"
ENV_EXAMPLE_FILE = BASE_DIR / ".env.example"
API_KEY_NAME = "YOUTUBE_API_KEY"

# 画面に表示する動画の数の上限
MAX_RESULTS = 10

# 並べ替えの候補として、検索で集める動画の数（1〜50。検索1回の使用量は件数によらず 100 ユニット）
SEARCH_COUNT = 50

# 何か月前までに投稿された動画を探すか（最近の流行を見るため）
PERIOD_MONTHS = 6

# この秒数以内の動画を「ショート動画」とみなす（ショート動画は最長3分）
SHORTS_MAX_SECONDS = 180

# 本物のショートか確かめるとき、YouTube のページの返事を待つ秒数
SHORTS_CHECK_TIMEOUT = 10

# 選べる動画の種類。番号: (画面に出す名前, ショートだけにするか)
# 何も入力せずに Enter を押したときは、DEFAULT_TYPE の番号を使う
TYPE_CHOICES = {
    "1": ("すべて", False),
    "2": ("ショートだけ", True),
}
DEFAULT_TYPE = "1"

# 動画のページの URL の形。{video_id} のところに動画IDが入る
VIDEO_URL = "https://www.youtube.com/watch?v={video_id}"
SHORTS_URL = "https://www.youtube.com/shorts/{video_id}"

# サムネイル画像の大きさの名前（大きい順）。この順に探して、最初にあったものを使う
THUMBNAIL_SIZES = ("maxres", "standard", "high", "medium", "default")

# CSV を保存するフォルダと、ファイル名の形（日時が入るので、前のファイルを上書きしない）
RESULTS_DIR = BASE_DIR / "results"
CSV_NAME = "youtube_{now:%Y%m%d_%H%M%S}.csv"

# CSV の列の名前（この順に並ぶ）
CSV_COLUMNS = [
    "順位", "タイトル", "チャンネル名", "登録者数", "再生回数", "バズ度", "いいね数", "いいね率（%）",
    "長さ（秒）", "投稿日", "タグ", "サムネイルURL", "動画URL", "検索キーワード", "種類", "検索日",
]

# 検索結果の区切り線
SEPARATOR = "-" * 30


# ============================================================
# F1 API キーの読み込み
# ============================================================
def load_api_key():
    """
    .env の YOUTUBE_API_KEY から API キーを読み込んで返す。
    .env が無い・キーが空の場合は、理由を表示して終了する（F8）。
    """
    # .env の中身を「環境変数」（プログラムが読める設定値）として読み込む
    load_dotenv(ENV_FILE)
    api_key = os.getenv(API_KEY_NAME, "").strip()

    if not api_key:
        show_error_and_exit(
            f"API キーが設定されていません。\n"
            f"{ENV_EXAMPLE_FILE.name} をコピーして「{ENV_FILE.name}」という名前にし、\n"
            f"{API_KEY_NAME}= の後ろに API キーを書いてください。\n"
            f"置き場所: {BASE_DIR}"
        )
    return api_key


# ============================================================
# F2 条件の入力
# ============================================================
def ask_keyword():
    """
    検索キーワードを入力してもらう。空欄なら、もう一度聞く。
    """
    while True:
        keyword = input("検索キーワードを入力してください: ").strip()
        if keyword:
            return keyword
        print("キーワードが空です。キーワードを入力してください。")


def ask_type():
    """
    動画の種類を番号で選んでもらい、(画面に出す名前, ショートだけにするか) を返す。
    何も入力せずに Enter なら「すべて」。番号以外なら、もう一度聞く。
    """
    choices = "  ".join(f"{number}. {name}" for number, (name, _) in TYPE_CHOICES.items())
    while True:
        print(f"動画の種類を選んでください: {choices}")
        answer = input(f"番号（Enter だけなら {TYPE_CHOICES[DEFAULT_TYPE][0]}）: ").strip()
        if answer == "":
            answer = DEFAULT_TYPE
        if answer in TYPE_CHOICES:
            return TYPE_CHOICES[answer]
        print(f"{'、'.join(TYPE_CHOICES)} のどれかを入力してください。")


# ============================================================
# F3 動画の検索
# ============================================================
def months_ago(today, months):
    """
    today から months か月前の日付を返す（例：10/24 の6か月前 → 4/24）。
    その月に同じ日が無いとき（8/31 の6か月前など）は、その月の最後の日にする。
    """
    month_index = today.year * 12 + (today.month - 1) - months
    year, month = divmod(month_index, 12)
    month += 1
    for day in range(today.day, 0, -1):
        try:
            return date(year, month, day)
        except ValueError:
            continue


def search_video_ids(youtube, keyword, since, shorts_only):
    """
    YouTube Data API の search.list で動画を検索し、見つかった動画ID のリストを返す。

    - type="video"            … チャンネルや再生リストを除き、動画だけにする
    - order="viewCount"       … 再生回数の多い動画を候補として集める（順番は正確でないので、後で並べ直す）
    - publishedAfter          … この日時より後に投稿された動画だけにする
    - videoDuration="short"   … ショートだけのとき、4分未満の動画だけにする
    """
    params = {
        "q": keyword,
        "part": "id",
        "type": "video",
        "order": "viewCount",
        "publishedAfter": f"{since.isoformat()}T00:00:00Z",
        "maxResults": SEARCH_COUNT,
    }
    if shorts_only:
        params["videoDuration"] = "short"

    response = youtube.search().list(**params).execute()
    return [item["id"]["videoId"] for item in response.get("items", [])]


# ============================================================
# F4 詳しい情報の取得
# ============================================================
def fetch_video_details(youtube, video_ids):
    """
    videos.list で、動画ID ごとの詳しい情報（タイトル・再生回数・長さ・タグなど）を取り、リストで返す。
    50件までなら1回で取れる（1ユニット）。
    - snippet         … タイトル・チャンネル・投稿日・タグ・サムネイル
    - statistics      … 再生回数・いいね数（非公開なら項目が無い）
    - contentDetails  … 動画の長さ（PT1M5S のような形）
    """
    if not video_ids:
        return []
    response = youtube.videos().list(
        part="snippet,statistics,contentDetails",
        id=",".join(video_ids),
        maxResults=len(video_ids),
    ).execute()
    return response.get("items", [])


def fetch_subscriber_counts(youtube, channel_ids):
    """
    channels.list で、チャンネルID ごとの登録者数を取り、{チャンネルID: 登録者数} の辞書で返す。
    登録者数を非公開にしているチャンネルは None にする。
    （YouTube は登録者数を上から3けたに丸めて返す。例：12,345人 → 12,300人）
    """
    unique_ids = list(dict.fromkeys(channel_ids))  # 同じチャンネルを1回だけにする（順番は保つ）
    if not unique_ids:
        return {}
    response = youtube.channels().list(
        part="statistics",
        id=",".join(unique_ids),
        maxResults=len(unique_ids),
    ).execute()

    counts = {}
    for channel in response.get("items", []):
        statistics = channel.get("statistics", {})
        if statistics.get("hiddenSubscriberCount") or "subscriberCount" not in statistics:
            counts[channel["id"]] = None
        else:
            counts[channel["id"]] = int(statistics["subscriberCount"])
    return counts


def parse_duration(text):
    """
    API の長さの書き方（ISO 8601。例：PT1M5S = 1分5秒、P0D = 0秒）を、秒数に直して返す。
    読めない形のときは 0 を返す。
    """
    match = re.fullmatch(r"P(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", text or "")
    if not match:
        return 0
    days, hours, minutes, seconds = (int(value or 0) for value in match.groups())
    return ((days * 24 + hours) * 60 + minutes) * 60 + seconds


def to_int(value):
    """
    API の数（"12345" のような文字列）を int にする。無い（非公開）ときは None にする。
    """
    return None if value is None else int(value)


def pick_thumbnail(thumbnails):
    """
    サムネイルの中から、一番大きい画像の URL を返す。無ければ空の文字列。
    """
    for size in THUMBNAIL_SIZES:
        if size in thumbnails:
            return thumbnails[size]["url"]
    return ""


def make_records(videos, subscribers):
    """
    videos.list の動画と登録者数から、表示と CSV に使う「1動画 = 1つの辞書」のリストを作って返す。
    バズ度・いいね率も、ここで計算する（計算できないときは None）。
    """
    records = []
    for video in videos:
        snippet = video["snippet"]
        statistics = video.get("statistics", {})
        seconds = parse_duration(video.get("contentDetails", {}).get("duration"))
        views = to_int(statistics.get("viewCount"))
        likes = to_int(statistics.get("likeCount"))
        subscriber_count = subscribers.get(snippet["channelId"])
        # 長さが0秒（ライブ配信中・配信予定）の動画は、ショートに含めない
        is_short = 0 < seconds <= SHORTS_MAX_SECONDS
        # 投稿日時は世界標準時（末尾の Z）で届くので、日本の時刻に直して日付だけにする
        published = datetime.fromisoformat(snippet["publishedAt"].replace("Z", "+00:00")).astimezone().date()

        records.append({
            "video_id": video["id"],
            "title": html.unescape(snippet["title"]),
            "channel": snippet["channelTitle"],
            "subscribers": subscriber_count,
            "views": views,
            "buzz": views / subscriber_count if views is not None and subscriber_count else None,
            "likes": likes,
            "like_rate": likes / views * 100 if likes is not None and views else None,
            "seconds": seconds,
            "is_short": is_short,
            "published": published,
            "tags": snippet.get("tags", []),
            "thumbnail": pick_thumbnail(snippet.get("thumbnails", {})),
            "url": (SHORTS_URL if is_short else VIDEO_URL).format(video_id=video["id"]),
        })
    return records


# ============================================================
# F5 絞り込みと並べ替え
# ============================================================
def is_real_short(video_id):
    """
    YouTube の /shorts/ のページを開いて、本物のショート動画か確かめる。
    ショートでない動画（3分以内の横長の動画など）は、ふつうの動画のページ（/watch?v=）に転送される。
    転送されたら False、それ以外は True を返す。
    （API ではなく YouTube のページの動きを使うので、YouTube の仕組みが変わると使えなくなることがある）
    """
    http = httplib2.Http(timeout=SHORTS_CHECK_TIMEOUT)
    http.follow_redirects = False  # 転送先に進まず、転送されたかどうかだけを見る
    # HEAD … ページの中身は受け取らず、返事の見出し（ステータスと転送先）だけをもらう
    response, _ = http.request(SHORTS_URL.format(video_id=video_id), "HEAD")
    return "/watch" not in response.get("location", "")


def filter_and_sort(records, shorts_only, check_short=is_real_short):
    """
    再生回数の多い順に並べ、上から MAX_RESULTS 件を返す。再生回数が非公開の動画は、一番下にする。
    3分以内の動画は、上から順に check_short で本物のショートか確かめ、
    ショートでなければ URL をふつうの動画の形に直す（ショートだけのときは除く）。
    確かめるのは、表示する分が集まるまでにする（50件全部は開かない）。
    """
    # 非公開（None）は -1 として並べ、一番下にする
    records = sorted(records, key=lambda record: record["views"] if record["views"] is not None else -1, reverse=True)

    results = []
    for record in records:
        if record["is_short"] and not check_short(record["video_id"]):
            record["is_short"] = False
            record["url"] = VIDEO_URL.format(video_id=record["video_id"])
        if shorts_only and not record["is_short"]:
            continue
        results.append(record)
        if len(results) == MAX_RESULTS:
            break
    return results


# ============================================================
# F6 結果の表示
# ============================================================
def format_count(number):
    """
    数を日本語で読みやすくする（5,678 / 85.3万 / 1,234万 / 1.2億）。None なら「非公開」。
    """
    if number is None:
        return "非公開"
    for unit_value, unit_name in ((10**8, "億"), (10**4, "万")):
        if number >= unit_value:
            value = number / unit_value
            # 100 未満は小数1けたまで（85.3万）、それ以上は整数（1,234万）
            text = f"{value:.1f}".rstrip("0").rstrip(".") if value < 100 else f"{value:,.0f}"
            return f"{text}{unit_name}"
    return f"{number:,}"


def format_duration(seconds):
    """
    秒数を「0:45」「12:05」「1:02:03」の形にする。
    """
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02}:{secs:02}" if hours else f"{minutes}:{secs:02}"


def show_results(keyword, type_name, since, records):
    """
    見つかった動画の番号・タイトル・チャンネル・再生回数・バズ度・長さ・投稿日・URL を表示する。
    """
    print()
    print(f"「{keyword}」の検索結果（{type_name}・{since:%Y/%m/%d} 以降・再生回数順・{len(records)}件）")
    print(SEPARATOR)
    for number, record in enumerate(records, start=1):
        subscribers = "非公開" if record["subscribers"] is None else f"{format_count(record['subscribers'])}人"
        views = "非公開" if record["views"] is None else f"{format_count(record['views'])}回"
        buzz = "—" if record["buzz"] is None else f"{record['buzz']:,.1f}倍"
        print(f"{number}. {record['title']}")
        print(f"   チャンネル: {record['channel']}（登録者 {subscribers}）")
        print(
            f"   再生回数 {views} ／ バズ度 {buzz} ／ "
            f"{format_duration(record['seconds'])} ／ {record['published']:%Y/%m/%d} 投稿"
        )
        print(f"   {record['url']}")
    print(SEPARATOR)


# ============================================================
# F7 CSV に保存
# ============================================================
def ask_save():
    """
    CSV に保存するか聞く。y なら True、n なら False を返す。
    """
    while True:
        answer = input("CSV に保存しますか？ (y/n): ").strip().lower()
        if answer in ("y", "n"):
            return answer == "y"
        print("y か n を入力してください。")


def round_or_blank(value, digits):
    """
    計算した値を小数 digits けたに丸める。計算できない（None）ときは空欄にする。
    """
    return "" if value is None else round(value, digits)


def save_csv(records, keyword, type_name):
    """
    検索結果を results/ フォルダに CSV で保存し、保存したファイルの場所を返す。
    - 文字コードは UTF-8（BOM 付き = utf-8-sig）。BOM が無いと Mac の Excel で日本語が文字化けする
    - 数の列は数字のまま入れる（表計算で並べ替え・計算できるように）。非公開なら空欄
    """
    now = datetime.now()
    RESULTS_DIR.mkdir(exist_ok=True)
    path = RESULTS_DIR / CSV_NAME.format(now=now)

    # newline="" は、csv モジュールが改行を正しく書くための決まりごと
    with path.open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.writer(file)
        writer.writerow(CSV_COLUMNS)
        for number, record in enumerate(records, start=1):
            writer.writerow([
                number,
                record["title"],
                record["channel"],
                "" if record["subscribers"] is None else record["subscribers"],
                "" if record["views"] is None else record["views"],
                round_or_blank(record["buzz"], 2),
                "" if record["likes"] is None else record["likes"],
                round_or_blank(record["like_rate"], 2),
                record["seconds"],
                record["published"].isoformat(),
                " / ".join(record["tags"]),
                record["thumbnail"],
                record["url"],
                keyword,
                type_name,
                now.strftime("%Y-%m-%d %H:%M"),
            ])
    return path


# ============================================================
# F8 エラー表示
# ============================================================
def show_error_and_exit(message):
    """
    原因が分かる日本語のエラーメッセージを表示して、プログラムを終了する。
    """
    print(f"エラー: {message}", file=sys.stderr)
    sys.exit(1)


def explain_http_error(error):
    """
    Google とのやりとりで起きたエラー（HttpError）を、日本語の説明に直して返す。
    """
    # Google から届いたエラーの中身（英語の理由が書いてある）
    content = error.content.decode("utf-8", errors="replace")

    # 1日の割り当て（クォータ）を使い切った
    if "quotaExceeded" in content:
        return (
            "今日の検索できる回数（割り当て）を使い切りました。\n"
            "割り当ては毎日、太平洋時間の0時（日本時間の16時か17時）にリセットされます。\n"
            f"詳細: {error.reason}"
        )
    # API キーが正しくない
    if error.status_code == 400 and "API key" in content:
        return (
            "API キーが正しくありません。\n"
            f"{ENV_FILE.name} の {API_KEY_NAME} が、Google Cloud Console で作ったキーと同じか確認してください。\n"
            f"詳細: {error.reason}"
        )
    # YouTube Data API が有効でない、またはキーの制限で使えない
    if error.status_code == 403:
        return (
            "YouTube Data API を使う許可がありません。\n"
            "Google Cloud Console で次を確認してください。\n"
            "  ・「API とサービス」→「ライブラリ」で「YouTube Data API v3」が有効になっているか\n"
            "  ・「認証情報」の API キーの制限に「YouTube Data API v3」が入っているか\n"
            f"詳細: {error.reason}"
        )
    return (
        "YouTube とのやりとりに失敗しました。\n"
        f"ステータスコード: {error.status_code}\n"
        f"詳細: {error.reason}"
    )


# ============================================================
# メインの処理（上の関数を順番に呼ぶ）
# ============================================================
def main():
    """
    プログラム全体の流れ：
    API キーの読み込み → キーワード・種類の入力 → 検索 → 詳しい情報の取得
    → 絞り込みと並べ替え → 結果の表示 → CSV に保存
    """
    try:
        # F1 API キーの読み込み（検索より先に確かめる）
        api_key = load_api_key()

        # F2 条件の入力
        keyword = ask_keyword()
        type_name, shorts_only = ask_type()

        # YouTube を操作するための「サービス」を作る
        youtube = build("youtube", "v3", developerKey=api_key)

        # F3 動画の検索（今日から PERIOD_MONTHS か月前より後の動画）
        since = months_ago(date.today(), PERIOD_MONTHS)
        print("検索しています...")
        video_ids = search_video_ids(youtube, keyword, since, shorts_only)

        # F4 詳しい情報の取得（動画 → チャンネルの登録者数）
        videos = fetch_video_details(youtube, video_ids)
        subscribers = fetch_subscriber_counts(youtube, [video["snippet"]["channelId"] for video in videos])
        records = make_records(videos, subscribers)

        # F5 絞り込みと並べ替え（本物のショートかも、ここで確かめる）
        print("ショート動画かどうか確かめています...")
        records = filter_and_sort(records, shorts_only)
        if not records:
            print("動画が見つかりませんでした。")
            return

        # F6 結果の表示
        show_results(keyword, type_name, since, records)

        # F7 CSV に保存
        if ask_save():
            try:
                path = save_csv(records, keyword, type_name)
            except OSError as error:
                show_error_and_exit(
                    f"CSV を保存できませんでした（{type(error).__name__}: {error.strerror}）。\n"
                    f"保存先のフォルダに書き込めるか確認してください: {RESULTS_DIR}"
                )
            print(f"保存しました: {path}")

    except HttpError as error:
        # Google とのやりとりで失敗した（F8）
        show_error_and_exit(explain_http_error(error))
    except (httplib2.HttpLib2Error, OSError) as error:
        # インターネットにつながらない など（F8）
        # 詳細は種類の名前だけにする。error の中身には API キー入りの URL が入ることがあるため
        show_error_and_exit(
            "YouTube につながりませんでした。インターネット接続を確認してください。\n"
            f"詳細: {type(error).__name__}"
        )
    except (KeyboardInterrupt, EOFError):
        # Ctrl + C で途中でやめた場合
        print("\n中断しました。")
        sys.exit(1)


# このファイルを直接実行したときだけ main() を動かす
if __name__ == "__main__":
    main()
