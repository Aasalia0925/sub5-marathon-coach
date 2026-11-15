#!/usr/bin/env python3
"""서브5 코치 알림 (GitHub Actions용).

intervals.icu API에서 웰니스·운동·캘린더를 읽어 규칙대로 판단하고 Slack 웹훅으로 보낸다.
캘린더는 읽기만 하고 수정하지 않는다.

사용법: python3 coach.py morning | weekly
환경 변수: INTERVALS_API_KEY, INTERVALS_ATHLETE_ID(없으면 0 = 본인), SLACK_WEBHOOK_URL
테스트용: COACH_DATE=YYYY-MM-DD (오늘 날짜 대신), COACH_DRY_RUN=1 (Slack 전송 없이 출력만)
"""
import base64
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

API = "https://intervals.icu/api/v1"
TZ = ZoneInfo(os.environ.get("COACH_TZ", "Asia/Seoul"))
RACE_DATE = dt.date(2027, 2, 28)
WEEKDAYS = "월화수목금토일"
RUN_TYPES = {"Run", "VirtualRun", "TrailRun"}
PAIN_LINE = "무릎·뒤꿈치 통증이 있으면 오늘 러닝은 쉬어요."


# ---------- 입출력 ----------

def api_get(path, params):
    key = os.environ["INTERVALS_API_KEY"].strip()
    if not key:
        raise RuntimeError("INTERVALS_API_KEY가 비어 있음")
    auth = base64.b64encode(f"API_KEY:{key}".encode()).decode()
    url = f"{API}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={
        "Authorization": f"Basic {auth}",
        "Accept": "application/json",
        "User-Agent": "sub5-coach-bot/1.0 (+github actions)",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read()[:300].decode("utf-8", "replace")
        print(f"intervals.icu 요청 실패: {path} → HTTP {e.code}\n{body}", file=sys.stderr)
        raise


def send_slack(text):
    if os.environ.get("COACH_DRY_RUN"):
        return
    body = json.dumps({"text": text[:3900]}).encode()
    req = urllib.request.Request(os.environ["SLACK_WEBHOOK_URL"], data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        if r.read().decode().strip() != "ok":
            raise RuntimeError("Slack 응답이 ok가 아님")


def load(today):
    aid = os.environ.get("INTERVALS_ATHLETE_ID") or "0"
    wellness = api_get(f"/athlete/{aid}/wellness",
                       {"oldest": (today - dt.timedelta(days=42)).isoformat(), "newest": today.isoformat()})
    activities = api_get(f"/athlete/{aid}/activities",
                         {"oldest": (today - dt.timedelta(days=27)).isoformat(), "newest": f"{today}T23:59:59"})
    events = api_get(f"/athlete/{aid}/events",
                     {"oldest": (today - dt.timedelta(days=7)).isoformat(),
                      "newest": (today + dt.timedelta(days=8)).isoformat()})
    W = {w["id"]: w for w in wellness if w.get("id")}
    return W, activities, events


# ---------- 계산 도우미 ----------

def mean(values):
    v = [float(x) for x in values if x is not None]
    return sum(v) / len(v) if v else None


def day_of(s):
    return (s or "")[:10]


def is_sub5(e):
    return "sub5" in (e.get("tags") or [])


def act_load(a):
    return a.get("icu_training_load") or 0


def ev_load(e):
    return e.get("icu_training_load") or 0


def fmt_day(d):
    return f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})"


def fmt_hours(h):
    if h is None:
        return "기록 없음"
    return f"{int(h)}시간 {round((h - int(h)) * 60):02d}분"


def fmt(x, digits=0):
    return "—" if x is None else f"{x:.{digits}f}"


def kind_of(e):
    tags = set(e.get("tags") or [])
    if e.get("category") == "RACE_A":
        return "race"
    if tags & {"marathon-pace", "tempo"}:
        return "quality"
    if "long-run" in tags:
        return "long"
    if "strength" in tags:
        return "strength"
    if "cross" in tags:
        return "cross"
    if e.get("type") in RUN_TYPES:
        return "easy"
    return "other"


ADVICE = {
    0: {
        "quality": "계획대로 하되 심박 155를 넘기지 마세요.",
        "long": "계획대로, 심박 145 이하로 편하게. 40분마다 보급하세요.",
        "easy": "계획대로, 심박 140 이하로 편하게.",
        "strength": "계획대로 하세요.",
        "cross": "계획대로, 심박 140 이하로.",
        "other": "계획대로 하세요.",
    },
    1: {
        "quality": "핵심 세션 대신 쉬운 러닝 30분(125-140bpm)으로 바꾸세요. 워치에서 계획 운동 대신 일반 달리기로 시작하면 돼요.",
        "long": "계획대로 하되 심박 140 이하로 더 천천히. 힘들면 거리를 줄이세요.",
        "easy": "계획대로 하되 심박 135 이하로.",
        "strength": "계획대로 하되 세트를 줄여도 돼요.",
        "cross": "가볍게 하거나 생략하세요.",
        "other": "가볍게 하세요.",
    },
    2: {
        "quality": "오늘은 러닝 대신 걷기 30분이나 휴식을 권해요.",
        "long": "롱런은 절반 거리로 줄이거나 내일로 미루세요.",
        "easy": "러닝 대신 걷기 30분이나 휴식을 권해요.",
        "strength": "2세트로 가볍게 하세요.",
        "cross": "오늘은 생략하세요.",
        "other": "오늘은 쉬세요.",
    },
}


# ---------- 아침 준비도 ----------

def morning(today, W, A, E):
    head_day = fmt_day(today)
    t = today.isoformat()

    if today == RACE_DATE:
        return "\n".join([
            f"🏁 *레이스 날 · {head_day}*",
            "• 0~5km: 7:15~7:20/km. 출발 흥분에 휩쓸리지 않기",
            "• 5~30km: 7:00~7:05/km. 심박 155 넘으면 속도 낮추기",
            "• 30km 이후: 버틸 수 있으면 유지, 힘들면 1분 걷기를 섞어도 5시간 안에 들어와요",
            "• 보급: 출발 30분 전 젤 1개, 이후 40분마다 젤 1개와 급수대마다 물 몇 모금",
            "15주 동안 쌓은 걸 믿고 즐기세요. 화이팅! 💪",
        ])

    rec = W.get(t, {})
    hist = [W[d] for d in sorted(W) if d < t]
    hrv_b = mean(h.get("hrv") for h in hist)
    rhr_b = mean(h.get("restingHR") for h in hist)
    hrv, rhr = rec.get("hrv"), rec.get("restingHR")
    sleep_h = rec["sleepSecs"] / 3600 if rec.get("sleepSecs") else None

    yday = (today - dt.timedelta(days=1)).isoformat()
    y_actual = sum(act_load(a) for a in A if day_of(a.get("start_date_local")) == yday)
    y_plan = sum(ev_load(e) for e in E if is_sub5(e) and e.get("category") == "WORKOUT"
                 and day_of(e.get("start_date_local")) == yday)
    unplanned = y_actual - y_plan

    todays = [e for e in E if is_sub5(e) and e.get("category") == "WORKOUT"
              and day_of(e.get("start_date_local")) == t]
    todays.sort(key=lambda e: 0 if e.get("type") in RUN_TYPES else 1)

    nodata = hrv is None and rhr is None
    flags = []
    if hrv is not None and hrv_b and hrv < hrv_b * 0.85:
        flags.append(f"HRV가 평소보다 {round((1 - hrv / hrv_b) * 100)}% 낮음")
    if rhr is not None and rhr_b and rhr >= rhr_b + 4:
        flags.append(f"안정시 심박이 평소보다 {rhr - rhr_b:.0f}bpm 높음")
    if sleep_h is not None and sleep_h < 5:
        flags.append(f"수면 {fmt_hours(sleep_h)}")
    if unplanned >= 40:
        flags.append(f"어제 계획보다 부하를 {unplanned:.0f} 더 함")

    if not todays:
        icon, verdict, todo = "🛌", "오늘은 휴식일이에요.", "푹 쉬고 잠을 충분히 자요."
    elif nodata:
        icon = "⏳"
        recent = [W[d] for d in sorted(W) if d < t and W[d].get("hrv") is not None][-3:]
        r_hrv = mean(r.get("hrv") for r in recent)
        trend = ""
        if r_hrv and hrv_b:
            trend = f" 최근 3일 HRV 평균 {r_hrv:.0f}ms(평소 {hrv_b:.0f})" + (
                "로 낮은 편이라 강도를 낮추는 게 좋아요." if r_hrv < hrv_b * 0.9 else "로 괜찮은 편이에요.")
        verdict = "오늘 데이터가 아직 동기화되지 않았어요. Garmin Connect 앱을 열어 동기화한 뒤 운동 전에 확인해 주세요." + trend
        todo = ADVICE[0][kind_of(todays[0])]
    else:
        n = min(len(flags), 2)
        icon = {0: "✅", 1: "🟡", 2: "🔴"}[n]
        verdict = {0: "회복 상태 괜찮아요. 계획대로 가요.",
                   1: "회복 신호가 하나 안 좋아요. 강도를 조금 낮춰요.",
                   2: "회복이 부족해요. 오늘은 무리하지 마세요."}[n]
        todo = ADVICE[n][kind_of(todays[0])]

    lines = [f"{icon} *오늘 준비도 · {head_day}*"]
    lines.append("• 오늘 운동: " + (" / ".join(e.get("name", "") for e in todays) if todays else "휴식일"))
    lines.append(f"• HRV {fmt(hrv)} ms (평소 {fmt(hrv_b)}) · 안정시 심박 {fmt(rhr)} (평소 {fmt(rhr_b)}) · 수면 {fmt_hours(sleep_h)}")
    lines.append(f"• 판단: {verdict}")
    if todays:
        lines.append(f"• 오늘 할 일: {todo}")
    if flags:
        lines.append("• 경고: " + ", ".join(flags))
    lines.append(f"레이스까지 {(RACE_DATE - today).days}일")
    lines.append(PAIN_LINE)
    return "\n".join(lines)


# ---------- 주간 리포트 ----------

def weekly(today, W, A, E):
    start = today - dt.timedelta(days=6)
    week = {(start + dt.timedelta(days=i)).isoformat() for i in range(7)}
    planned = [e for e in E if is_sub5(e) and e.get("category") == "WORKOUT"
               and day_of(e.get("start_date_local")) in week]
    acts7 = [a for a in A if day_of(a.get("start_date_local")) in week]
    act_days = {day_of(a.get("start_date_local")) for a in acts7}
    done = [e for e in planned if day_of(e.get("start_date_local")) in act_days]
    missed = [e for e in planned if day_of(e.get("start_date_local")) not in act_days]

    plan_load = sum(ev_load(e) for e in planned)
    actual = sum(act_load(a) for a in acts7)
    chronic = sum(act_load(a) for a in A) / 4
    acwr = actual / chronic if chronic else None

    runs = [a for a in acts7 if a.get("type") in RUN_TYPES]
    longest = max(runs, key=lambda a: a.get("distance") or 0, default=None)
    long_plan = next((e for e in planned if "long-run" in (e.get("tags") or [])), None)

    t = today.isoformat()
    days = sorted(W)
    last7 = [W[d] for d in days if d in week]
    hrv7, hrv42 = mean(w.get("hrv") for w in last7), mean(W[d].get("hrv") for d in days)
    rhr7, rhr42 = mean(w.get("restingHR") for w in last7), mean(W[d].get("restingHR") for d in days)
    sleep7 = mean((w.get("sleepSecs") or 0) / 3600 if w.get("sleepSecs") else None for w in last7)

    warnings = []
    if hrv7 and hrv42 and hrv7 < hrv42 * 0.9:
        warnings.append(f"HRV 7일 평균이 평소보다 {round((1 - hrv7 / hrv42) * 100)}% 낮음")
    if rhr7 and rhr42 and rhr7 >= rhr42 + 3:
        warnings.append(f"안정시 심박이 평소보다 {rhr7 - rhr42:.1f}bpm 높음")
    if sleep7 is not None and sleep7 < 5:
        warnings.append(f"수면 평균 {fmt_hours(sleep7)}")
    if acwr and acwr > 1.5:
        warnings.append(f"ACWR {acwr:.2f}로 부하 급증")
    if plan_load and actual > plan_load * 1.25:
        warnings.append(f"계획보다 부하를 {round((actual / plan_load - 1) * 100)}% 더 함")

    nxt_days = {(today + dt.timedelta(days=i)).isoformat() for i in range(1, 8)}
    nxt = sorted([e for e in E if is_sub5(e) and e.get("category") == "WORKOUT"
                  and day_of(e.get("start_date_local")) in nxt_days],
                 key=lambda e: e.get("start_date_local"))

    lines = [f"📋 *서브5 주간 리포트 · {start.month}/{start.day}~{today.month}/{today.day}*", ""]
    lines.append("*지난주*")
    lines.append(f"• 계획 운동 {len(done)}/{len(planned)}회 완료" + (
        " (빠진 운동: " + ", ".join(fmt_day(dt.date.fromisoformat(day_of(e['start_date_local']))) + " " + e.get('name', '') for e in missed) + ")" if missed else ""))
    lines.append(f"• 부하 {actual:.0f} (계획 {plan_load:.0f}) · ACWR {fmt(acwr, 2)}")
    if longest:
        lr = f"{(longest.get('distance') or 0) / 1000:.1f}km"
        lines.append(f"• 가장 긴 러닝 {lr}" + (f" (계획: {long_plan.get('name')})" if long_plan else ""))
    lines.append("")
    lines.append("*회복*")
    lines.append(f"• HRV {fmt(hrv7)} ms (평소 {fmt(hrv42)}) · 안정시 심박 {fmt(rhr7, 1)} (평소 {fmt(rhr42, 1)}) · 수면 {fmt_hours(sleep7)}")
    lines.append("• 경고: " + (", ".join(warnings) if warnings else "없음 ✅"))
    lines.append("")
    lines.append("*다음 주 계획*")
    for e in nxt:
        d = dt.date.fromisoformat(day_of(e["start_date_local"]))
        lines.append(f"• {fmt_day(d)} {e.get('name', '')}")
    if not nxt:
        lines.append("• 등록된 운동이 없어요")
    if warnings:
        lines.append("")
        lines.append("⚠️ 경고가 있어 다음 주 강도를 낮추는 게 좋아요. 맥의 Claude 주간 점검이 계획을 조정하고, 바뀌면 따로 알려 드려요.")
    lines.append("")
    lines.append(f"레이스까지 {(RACE_DATE - today).days}일 · 무릎·뒤꿈치 통증이 있으면 Claude에게 알려 주세요.")
    return "\n".join(lines)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "morning"
    today = dt.date.fromisoformat(os.environ["COACH_DATE"]) if os.environ.get("COACH_DATE") else dt.datetime.now(TZ).date()
    try:
        W, A, E = load(today)
    except Exception as e:
        send_slack(f"⚠️ *서브5 코치* intervals.icu 데이터를 읽지 못했어요 ({type(e).__name__}). GitHub Actions 기록을 확인해 주세요.")
        raise
    text = weekly(today, W, A, E) if mode == "weekly" else morning(today, W, A, E)
    print(text)
    send_slack(text)


if __name__ == "__main__":
    main()
