
import os
import json
import time
import calendar
import logging
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import gspread
from google.oauth2.service_account import Credentials


# ==========================================
# 基本設定
# ==========================================

API_VERSION = "v26.0"
API_BASE = f"https://graph.facebook.com/{API_VERSION}"

SECRET_NAME = "APP_SECRET_JSON"
SHEET_NAME = "gitreport"

MEDIA_NAME = "Meta"

# 標準ウェブサイトLeadイベント
LEAD_ACTION_TYPES = {
    "offsite_conversion.fb_pixel_lead"
}

ATTRIBUTION_WINDOWS = ["1d_view", "7d_click"]

HEADERS = [
    "media",
    "scope",
    "month",
    "day",
    "campaign_name",
    "adset_name",
    "ad_name",
    "gender",
    "age",
    "platform",
    "hour",
    "cv_view_1d",
    "cv_click_7d",
]

SCOPE_ORDER = [
    "month",
    "day",
    "ad",
    "gender",
    "age",
    "pf",
    "hour",
]

BREAKDOWNS = {
    "month": None,
    "day": None,
    "ad": None,
    "gender": "gender",
    "age": "age",
    "pf": "publisher_platform",
    "hour": "hourly_stats_aggregated_by_advertiser_time_zone",
}

# 日別・時間帯別のみ日単位
DAILY_SCOPES = {"day", "hour"}

# API取得設定
REQUEST_TIMEOUT = 120
MAX_RETRIES = 5
PAGE_LIMIT = 500

# スプレッドシート書き込み
WRITE_CHUNK_SIZE = 500

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

logger = logging.getLogger(__name__)

session = requests.Session()


# ==========================================
# Secrets読み込み
# ==========================================

def load_config():
    raw = os.environ.get(SECRET_NAME)

    if not raw:
        raise RuntimeError(
            f"GitHub Secretsの{SECRET_NAME}が設定されていません。"
        )

    try:
        config = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"{SECRET_NAME}が正しいJSON形式ではありません。"
        ) from exc

    try:
        meta = config["meta"]
        sheets = config["sheets"]
        service_account = config["gcp_service_account"]

        token = meta["token"]
        account_id = str(meta["account_id"])
        spreadsheet_id = sheets["spreadsheet_id"]

        if not token or not account_id or not spreadsheet_id:
            raise ValueError("必須項目が空欄です。")

        if not service_account.get("client_email"):
            raise ValueError("client_emailがありません。")

        if not service_account.get("private_key"):
            raise ValueError("private_keyがありません。")

    except (KeyError, ValueError, TypeError) as exc:
        raise RuntimeError(
            "Secretsの設定項目が不足しています。"
        ) from exc

    # JSON内で改行が\\nになっている場合に対応
    service_account["private_key"] = (
        service_account["private_key"].replace("\\n", "\n")
    )

    if account_id.startswith("act_"):
        account_id = account_id[4:]

    return token, account_id, spreadsheet_id, service_account


# ==========================================
# Meta API通信
# ==========================================

def meta_get(url, token, params=None):
    headers = {
        "Authorization": f"Bearer {token}"
    }

    for attempt in range(MAX_RETRIES):
        try:
            response = session.get(
                url,
                headers=headers,
                params=params,
                timeout=REQUEST_TIMEOUT,
            )

            if response.status_code in (429, 500, 502, 503, 504):
                raise requests.HTTPError(
                    f"一時的なAPIエラー: HTTP {response.status_code}"
                )

            if response.status_code >= 400:
                try:
                    error = response.json().get("error", {})
                    message = error.get("message", response.text[:300])
                    code = error.get("code", "")
                except ValueError:
                    message = response.text[:300]
                    code = ""

                # トークン・パラメータのエラーは再試行しない
                raise RuntimeError(
                    f"Meta APIエラー HTTP {response.status_code}"
                    f" / code={code}: {message}"
                )

            return response.json()

        except (requests.Timeout, requests.ConnectionError,
                requests.HTTPError) as exc:

            if attempt == MAX_RETRIES - 1:
                raise RuntimeError(
                    f"Meta API取得に失敗しました: {exc}"
                ) from exc

            wait = min(2 ** (attempt + 1), 60)

            logger.warning(
                "API再試行 %s/%s（%s秒後）",
                attempt + 1,
                MAX_RETRIES,
                wait,
            )

            time.sleep(wait)

    raise RuntimeError("Meta APIの再試行回数を超えました。")


def get_account_timezone(token, account_id):
    url = f"{API_BASE}/act_{account_id}"

    data = meta_get(
        url,
        token,
        {"fields": "timezone_name"},
    )

    timezone_name = data.get("timezone_name")

    if not timezone_name:
        raise RuntimeError(
            "広告アカウントのタイムゾーンを取得できませんでした。"
        )

    logger.info("広告アカウントのタイムゾーン: %s", timezone_name)

    return ZoneInfo(timezone_name)


# ==========================================
# 日付処理
# ==========================================

def first_day_of_month(target):
    return target.replace(day=1)


def add_months(target, months):
    year = target.year + (target.month - 1 + months) // 12
    month = (target.month - 1 + months) % 12 + 1

    day = min(
        target.day,
        calendar.monthrange(year, month)[1],
    )

    return date(year, month, day)


def get_month_ranges(start, end):
    ranges = []
    current = first_day_of_month(start)

    while current <= end:
        month_end = add_months(current, 1) - timedelta(days=1)

        since = max(current, start)
        until = min(month_end, end)

        if since <= until:
            ranges.append((since, until))

        current = add_months(current, 1)

    return ranges


def get_periods(timezone):
    today = datetime.now(timezone).date()
    yesterday = today - timedelta(days=1)

    current_month = first_day_of_month(today)

    # 当月を含めた直近12ヶ月
    month_start = add_months(current_month, -11)

    # 当月を含めた直近2ヶ月
    detail_start = add_months(current_month, -1)

    return {
        "month": (month_start, yesterday),
        "detail": (detail_start, yesterday),
    }


def to_sheet_date(value):
    """
    Google Sheetsの日付シリアル値へ変換。
    RAWで書き込み、DATE書式を設定する。
    """
    if not value:
        return ""

    if isinstance(value, str):
        value = date.fromisoformat(value)

    origin = date(1899, 12, 30)
    return (value - origin).days


# ==========================================
# Insights取得
# ==========================================

def fetch_insights(
    token,
    account_id,
    scope,
    since,
    until,
):
    url = f"{API_BASE}/act_{account_id}/insights"

    params = {
        "level": "ad",
        "fields": (
            "date_start,date_stop,"
            "campaign_name,adset_name,ad_name,"
            "actions"
        ),
        "time_range": json.dumps({
            "since": since.isoformat(),
            "until": until.isoformat(),
        }),
        "action_attribution_windows": json.dumps(
            ATTRIBUTION_WINDOWS
        ),
        "action_breakdowns": "action_type",
        "action_report_time": "impression",
        "limit": PAGE_LIMIT,
    }

    # 日別・時間帯別は1日ごとに集計
    params["time_increment"] = (
        1 if scope in DAILY_SCOPES else "all_days"
    )

    breakdown = BREAKDOWNS[scope]

    if breakdown:
        params["breakdowns"] = breakdown

    all_data = []

    while url:
        result = meta_get(url, token, params)

        all_data.extend(result.get("data", []))

        url = result.get("paging", {}).get("next")

        # paging.nextには検索条件が含まれる
        params = None

    return all_data


# ==========================================
# CV取得
# ==========================================

def extract_lead_cv(actions):
    view_1d = 0.0
    click_7d = 0.0

    for action in actions or []:
        action_type = action.get("action_type", "")

        if action_type not in LEAD_ACTION_TYPES:
            continue

        view_1d += float(action.get("1d_view") or 0)
        click_7d += float(action.get("7d_click") or 0)

    return (
        int(view_1d) if view_1d.is_integer() else view_1d,
        int(click_7d) if click_7d.is_integer() else click_7d,
    )


# ==========================================
# データ行作成
# ==========================================

def build_row(scope, data):
    date_start = data.get("date_start")

    if not date_start:
        raise RuntimeError(
            f"{scope}のデータにdate_startがありません。"
        )

    actual_date = date.fromisoformat(date_start)
    month_date = first_day_of_month(actual_date)

    # 日付列
    month_value = to_sheet_date(month_date)

    day_value = (
        to_sheet_date(actual_date)
        if scope in DAILY_SCOPES
        else ""
    )

    # ディメンション
    campaign_name = data.get("campaign_name", "")
    adset_name = data.get("adset_name", "")
    ad_name = data.get("ad_name", "")

    gender = data.get("gender", "")
    age = data.get("age", "")
    platform = data.get("publisher_platform", "")

    hour = data.get(
        "hourly_stats_aggregated_by_advertiser_time_zone",
        "",
    )

    # CV
    cv_view_1d, cv_click_7d = extract_lead_cv(
        data.get("actions", [])
    )

    return [
        MEDIA_NAME,
        scope,
        month_value,
        day_value,
        campaign_name,
        adset_name,
        ad_name,
        gender,
        age,
        platform,
        hour,
        cv_view_1d,
        cv_click_7d,
    ]


# ==========================================
# カテゴリ別データ取得
# ==========================================

def collect_all_data(token, account_id, timezone):
    periods = get_periods(timezone)

    all_rows = []

    for scope in SCOPE_ORDER:
        if scope == "month":
            start, end = periods["month"]
        else:
            start, end = periods["detail"]

        logger.info(
            "取得開始 scope=%s / %s ～ %s",
            scope,
            start,
            end,
        )

        # 月単位に分割してAPI取得
        ranges = get_month_ranges(start, end)

        scope_rows = []

        for since, until in ranges:
            logger.info(
                "取得中 scope=%s / %s ～ %s",
                scope,
                since,
                until,
            )

            records = fetch_insights(
                token,
                account_id,
                scope,
                since,
                until,
            )

            for record in records:
                scope_rows.append(
                    build_row(scope, record)
                )

        # 月・日・広告名などでソート
        scope_rows.sort(
            key=lambda row: (
                row[2],
                row[3] if isinstance(row[3], int) else -1,
                str(row[4]),
                str(row[5]),
                str(row[6]),
                str(row[7]),
                str(row[8]),
                str(row[9]),
                str(row[10]),
            )
        )

        logger.info(
            "取得完了 scope=%s / %s行",
            scope,
            len(scope_rows),
        )

        all_rows.extend(scope_rows)

    return all_rows


# ==========================================
# Google Sheets認証
# ==========================================

def connect_sheet(spreadsheet_id, service_account):
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
    ]

    credentials = Credentials.from_service_account_info(
        service_account,
        scopes=scopes,
    )

    client = gspread.authorize(credentials)

    spreadsheet = client.open_by_key(spreadsheet_id)

    try:
        worksheet = spreadsheet.worksheet(SHEET_NAME)
    except gspread.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=SHEET_NAME,
            rows=1000,
            cols=len(HEADERS),
        )

    return worksheet


# ==========================================
# スプレッドシート書き込み
# ==========================================

def write_to_sheet(worksheet, data_rows):
    rows = [HEADERS] + data_rows

    required_rows = max(len(rows), 1000)
    required_cols = len(HEADERS)

    # 必要な行数・列数を確保
    if worksheet.row_count < required_rows:
        worksheet.add_rows(
            required_rows - worksheet.row_count
        )

    if worksheet.col_count < required_cols:
        worksheet.add_cols(
            required_cols - worksheet.col_count
        )

    # 取得が完了してから既存データを消去
    logger.info("既存データをクリアします。")
    worksheet.clear()

    # C・D列を日付形式に設定
    date_format = {
        "numberFormat": {
            "type": "DATE",
            "pattern": "yyyy-mm-dd",
        }
    }

    worksheet.format("C:D", date_format)

    # L・M列は数値
    worksheet.format(
        "L:M",
        {
            "numberFormat": {
                "type": "NUMBER",
                "pattern": "0.##",
            }
        },
    )

    # 分割して書き込み
    for start in range(0, len(rows), WRITE_CHUNK_SIZE):
        chunk = rows[start:start + WRITE_CHUNK_SIZE]

        start_row = start + 1
        end_row = start + len(chunk)

        target_range = (
            f"A{start_row}:M{end_row}"
        )

        worksheet.update(
            range_name=target_range,
            values=chunk,
            value_input_option="RAW",
        )

        logger.info(
            "書き込み完了 %s",
            target_range,
        )

    # ヘッダー固定
    worksheet.freeze(rows=1)

    logger.info(
        "gitreport出力完了 / データ%s行",
        len(data_rows),
    )


# ==========================================
# メイン処理
# ==========================================

def main():
    logger.info("Meta広告レポート処理開始")

    token, account_id, spreadsheet_id, service_account = (
        load_config()
    )

    # 広告アカウントのタイムゾーンを取得
    timezone = get_account_timezone(
        token,
        account_id,
    )

    # 全カテゴリ取得
    # 途中でエラーになった場合は書き込まない
    rows = collect_all_data(
        token,
        account_id,
        timezone,
    )

    logger.info("全カテゴリ取得完了 / 合計%s行", len(rows))

    # Google Sheets接続
    worksheet = connect_sheet(
        spreadsheet_id,
        service_account,
    )

    # 出力
    write_to_sheet(
        worksheet,
        rows,
    )

    logger.info("すべての処理が正常に完了しました。")


if __name__ == "__main__":
    main()
