import json
import discord
from discord.ext import commands, tasks
import sqlite3
import datetime

# config.json 파일에서 BOT_TOKEN과 LOG_CHANNEL_ID 불러오기
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)
BOT_TOKEN = config["BOT_TOKEN"]
LOG_CHANNEL_ID = config["LOG_CHANNEL_ID"]

# Intents 설정: 메시지 내용, 음성 상태, 멤버 이벤트 접근
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# 데이터베이스 파일 (voice_records.db, 없으면 자동 생성)
DB_FILE = "voice_records.db"

def init_db():
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS voice_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            username TEXT,
            channel_name TEXT,
            join_time TEXT,
            leave_time TEXT,
            duration INTEGER
        )
    """)
    conn.commit()
    conn.close()

# 초를 HH:MM:SS 형식 문자열로 변환
def seconds_to_time(sec):
    hours = sec // 3600
    minutes = (sec % 3600) // 60
    seconds = sec % 60
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"

# 오늘 누적 공부시간 계산 (UTC 기준, DB의 오늘 날짜 레코드 합산)
def get_daily_total(member_id):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    cur.execute("""
        SELECT SUM(duration) FROM voice_logs
        WHERE user_id = ? AND substr(join_time, 1, 10) = ?
    """, (member_id, today))
    result = cur.fetchone()
    conn.close()
    total_seconds = result[0] if result[0] is not None else 0
    return seconds_to_time(total_seconds), total_seconds

# 전역 변수:
# voice_join_times: 현재 재시작 후 진행 중인 구간의 입장 시간 및 채널 (재시작 전에는 저장되어 있음)
voice_join_times = {}
# paused_accumulated: 일시정지 시 누적된 공부시간(초) [여러 번 일시정지시 누적]
paused_accumulated = {}
# last_summary_date: 자동 최종 요약 메시지 전송일 (KST 기준)
last_summary_date = None

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")
    init_db()
    log_channel = bot.get_channel(LOG_CHANNEL_ID)
    if log_channel:
        await log_channel.send("🌟 봇이 정상적으로 실행되었습니다!")
    else:
        print("🚫 LOG_CHANNEL_ID 채널을 찾을 수 없습니다.")
    daily_summary_task.start()

@bot.event
async def on_voice_state_update(member, before, after):
    now = datetime.datetime.utcnow()  # UTC 기준 현재시간
    log_channel = bot.get_channel(LOG_CHANNEL_ID)

    # [입장] 음성 채널 입장시:
    if before.channel is None and after.channel is not None:
        # 기록: 새 입장 시간 저장 (재시작 전/후 상관없이 새로 시작)
        voice_join_times[member.id] = {"join_time": now, "channel": after.channel}
        # 조회: 오늘 누적 공부시간 (DB 기록; 없으면 00:00:00)
        daily_time, _ = get_daily_total(member.id)
        message = f"▶️ {member.display_name}의 공부 시작 !\n- {member.display_name} : {daily_time}"
        print(message)
        if log_channel:
            await log_channel.send(message)

    # [퇴장] 음성 채널 퇴장 시:
    elif before.channel is not None and after.channel is None:
        # Case 1: 정상 진행(또는 재시작 후 완료): voice_join_times에 기록이 있음.
        if member.id in voice_join_times:
            record = voice_join_times.pop(member.id)
            join_time = record["join_time"]
            voice_channel = record["channel"]
            current_duration = int((now - join_time).total_seconds())
            save_voice_record(member, voice_channel, join_time, now, current_duration)
            # 만약 재시작(일시정지 후)의 값가 있다면, 누적값과 현재 지속을 합산하여 표시.
            if member.id in paused_accumulated:
                total_session = paused_accumulated[member.id] + current_duration
                resumed_str = seconds_to_time(current_duration)
                final_session_str = seconds_to_time(total_session)
                del paused_accumulated[member.id]
                daily_time, _ = get_daily_total(member.id)
                final_message = (
                    f"⏹️ {member.display_name}의 공부 종료!\n"
                    f"- 이번 세션 공부시간 : {final_session_str} (+ {resumed_str})\n"
                    f"- 오늘 누적 공부시간 : {daily_time}"
                )
            else:
                session_time_str = seconds_to_time(current_duration)
                daily_time, _ = get_daily_total(member.id)
                final_message = (
                    f"⏹️ {member.display_name}의 공부 종료!\n"
                    f"- 이번 세션 공부시간 : {session_time_str}\n"
                    f"- 오늘 누적 공부시간 : {daily_time}"
                )
            print(final_message)
            if log_channel:
                await log_channel.send(final_message)
        # Case 2: 사용자가 !일시정지 후 재시작 없이 바로 퇴장한 경우
        elif member.id in paused_accumulated:
            session_duration = paused_accumulated.pop(member.id)
            daily_time, _ = get_daily_total(member.id)
            final_message = (
                f"⏹️ {member.display_name}의 공부 종료!\n"
                f"- 이번 세션 공부시간 : {seconds_to_time(session_duration)}\n"
                f"- 오늘 누적 공부시간 : {daily_time}"
            )
            print(final_message)
            if log_channel:
                await log_channel.send(final_message)

    # [채널 이동] 음성 채널 간 이동 시: 입장 시각은 유지, 채널 정보만 업데이트
    elif before.channel is not None and after.channel is not None and before.channel != after.channel:
        voice_join_times[member.id]["channel"] = after.channel
        print(f"{member.display_name}님이 채널 이동: {before.channel.name} -> {after.channel.name}.")

def save_voice_record(member, channel, join_time, leave_time, duration):
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO voice_logs (user_id, username, channel_name, join_time, leave_time, duration)
        VALUES (?, ?, ?, ?, ?, ?)
    """, (
        member.id,
        str(member),
        channel.name,
        join_time.strftime("%Y-%m-%d %H:%M:%S"),
        leave_time.strftime("%Y-%m-%d %H:%M:%S"),
        duration
    ))
    conn.commit()
    conn.close()

# [일시정지] 명령어: !일시정지 @유저
@bot.command(name="일시정지")
async def pause_study(ctx, member: discord.Member = None):
    if member is None:
        await ctx.send("❗️ 유저를 멘션해야 합니다. 예: !일시정지 @철수")
        return
    now = datetime.datetime.utcnow()
    if member.id in voice_join_times:
        record = voice_join_times.pop(member.id)
        join_time = record["join_time"]
        new_pause = int((now - join_time).total_seconds())
        save_voice_record(member, record["channel"], join_time, now, new_pause)
        # 누적: 만약 이미 일시정지 기록이 있다면 더함; 없으면 새로 저장.
        if member.id in paused_accumulated:
            accumulated = paused_accumulated[member.id] + new_pause
            # 메시지에 이번 일시정지 구간(new_pause) 표시
            paused_accumulated[member.id] = accumulated
            msg = f"⏸️ {member.display_name}의 공부 일시정지 완료!\n- 이번 세션 공부시간 : {seconds_to_time(accumulated)} (+ {seconds_to_time(new_pause)})"
        else:
            paused_accumulated[member.id] = new_pause
            msg = f"⏸️ {member.display_name}의 공부 일시정지 완료!\n- 이번 세션 공부시간 : {seconds_to_time(new_pause)}"
        await ctx.send(msg)
    else:
        await ctx.send(f"❗️ {member.display_name}님은 현재 공부 중이 아닙니다.")

# [재시작] 명령어: !재시작 @유저
# 재시작 시에는 paused_accumulated에 저장된 누적 시간이 그대로 남아 있어, 이후 퇴장 시 합산할 수 있도록 함.
@bot.command(name="재시작")
async def resume_study(ctx, member: discord.Member = None):
    if member is None:
        await ctx.send("❗️ 유저를 멘션해야 합니다. 예: !재시작 @철수")
        return
    if member.id not in paused_accumulated:
        await ctx.send(f"❗️ {member.display_name}님은 일시정지 상태가 아닙니다.")
        return
    if member.voice is None or member.voice.channel is None:
        await ctx.send(f"❗️ {member.display_name}님은 음성 채널에 접속해 있지 않습니다.\n음성 채널에 들어간 후 재시작 명령어를 사용해주세요.")
        return
    now = datetime.datetime.utcnow()
    # 재시작 시, 새 입장 시각 기록 (paused_accumulated는 그대로 유지)
    voice_join_times[member.id] = {"join_time": now, "channel": member.voice.channel}
    # 재시작 메시지: 기존 paused_accumulated 값 출력
    msg = f"▶️ {member.display_name}의 공부 재시작!\n- 이번 세션 공부시간 : {seconds_to_time(paused_accumulated[member.id])}"
    await ctx.send(msg)

# [오늘 공부시간] 명령어: !오늘공부시간 (멘션 없으면 전체, 멘션 있으면 개별)
@bot.command(name="오늘공부시간")
async def show_daily(ctx, member: discord.Member = None):
    if member is not None:
        daily_time, _ = get_daily_total(member.id)
        await ctx.send(f"- {member.display_name} : {daily_time}")
    else:
        message_lines = ["✅ 오늘 공부시간"]
        for m in ctx.guild.members:
            if not m.bot:
                d_time, _ = get_daily_total(m.id)
                message_lines.append(f"- {m.display_name} : {d_time}")
        await ctx.send("\n".join(message_lines))

# [오늘 공부시간 초기화] 명령어: !초기화 @유저 (멘션 필수)
@bot.command(name="초기화")
async def reset_daily(ctx, member: discord.Member = None):
    if member is None:
        await ctx.send("❗️ 유저를 멘션하여야 합니다. 예: !초기화 @철수")
        return
    conn = sqlite3.connect(DB_FILE)
    cur = conn.cursor()
    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    cur.execute("DELETE FROM voice_logs WHERE user_id = ? AND substr(join_time,1,10) = ?", (member.id, today))
    conn.commit()
    conn.close()
    await ctx.send(f"🔄️ {member.display_name}의 오늘 공부시간 초기화 완료!\n- {member.display_name} : 00:00:00")

# [자동 최종 요약 및 초기화] 배경 작업:
# 매 10초마다 실행, KST 기준 23:59:59에 최종 요약 전송 후 DB 및 관련 변수 초기화
@tasks.loop(seconds=10)
async def daily_summary_task():
    global last_summary_date
    now_kst = datetime.datetime.utcnow() + datetime.timedelta(hours=9)
    if now_kst.hour == 23 and now_kst.minute == 59 and now_kst.second >= 59:
        if last_summary_date != now_kst.date():
            header = f"🏷️ [{now_kst.strftime('%Y.%m.%d')}] :"
            message_lines = [header]
            for guild in bot.guilds:
                for m in guild.members:
                    if not m.bot:
                        d_time, _ = get_daily_total(m.id)
                        message_lines.append(f"- {m.display_name} : {d_time}")
            summary_msg = "\n".join(message_lines)
            log_channel = bot.get_channel(LOG_CHANNEL_ID)
            if log_channel:
                await log_channel.send(summary_msg)
            # 자동 초기화: DB 전체 삭제, voice_join_times 및 paused_accumulated 초기화, 최신 입장 시간 업데이트
            conn = sqlite3.connect(DB_FILE)
            cur = conn.cursor()
            cur.execute("DELETE FROM voice_logs")
            conn.commit()
            conn.close()
            new_time = datetime.datetime.utcnow()
            for user_id in list(voice_join_times.keys()):
                voice_join_times[user_id]["join_time"] = new_time
            paused_accumulated.clear()
            last_summary_date = now_kst.date()

bot.run(BOT_TOKEN)