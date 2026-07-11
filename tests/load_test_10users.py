"""10인 동시 사용 부하테스트 — web_report 업로드/조회 혼합 시나리오 실측.

서버 8cpu/32GB 에서 동시 10명(업로드 2 + 조회 4 + 분포 2 + 경량 2)이 몰릴 때의
CPU/RSS 순간피크와 엔드포인트별 응답시간(p50/p95/max)을 측정한다. 15초 초과 요청은
!! SLOW 로 강조. 업로드는 lot_id 를 매번 유니크하게 부여해 analysis_key 를 바꿔
인메모리/디스크 캐시를 전부 콜드로 만든다 (최악 시나리오).

의존성: 서버 venv 그대로 (psutil/pandas/pyarrow — requests 불필요, urllib multipart).

사용 (repo 루트에서):
    server\\.venv\\Scripts\\python.exe tests\\load_test_10users.py --base http://127.0.0.1:8001
옵션:
    --duration N   부하 유지 시간(초, 기본 180)
    --items N      항목(측정 컬럼) 수 (기본 500)
    --dies N       die(데이터 행) 수 (기본 1000)
    --pid N        서버 프로세스 PID (미지정 시 --port LISTEN 프로세스 자동 탐지)
    --port N       자동 탐지용 포트 (기본 --base 의 포트)
    --admin-secret 관리자 패널 secret (기본 pte) — 종료 후 metrics 교차 확인용

주의: 실 DB/업로드 디렉토리에 LOADTEST 세션이 생성된다. 종료 후 관리자 패널
세션 탭에서 product="LOADTEST" 검색해 일괄 삭제할 것 (자동 삭제하지 않음).
"""
import argparse
import io
import json
import random
import statistics
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

import psutil

# 한국어 Windows 콘솔(cp949)에서 em-dash 등 출력 깨짐 방지
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

from web_report.honeyform import (  # noqa: E402
    META_COLUMNS, META_ROW_LABELS, encode_honeyform_parquet)

SLOW_SEC = 15.0


# ── honeyform 합성 payload ───────────────────────────────────────────────────

def make_honeyform_df(n_items, n_dies, seed=0):
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(seed)
    items = [f"item_{i:04d}" for i in range(1, n_items + 1)]

    # 상단 6행: 첫 컬럼에 라벨, item 컬럼에 항목 메타 (전부 문자열 — encode 가 string 변환)
    meta_rows = []
    for label in META_ROW_LABELS:
        row = {c: "" for c in META_COLUMNS}
        row["SERIAL"] = label
        for i, it in enumerate(items, start=1):
            row[it] = {"TSEQ": str(i), "TNO": str(i), "STEP": "FT",
                       "UNIT": "V", "HILIM": "1.5", "LOLIM": "0.5"}[label]
        meta_rows.append(row)

    # 데이터 행: 격자 좌표 + BIN1 95% / BIN2 5% (fail die 는 임의 항목이 limit 밖 + FAILTNO 귀속)
    values = rng.normal(1.0, 0.1, size=(n_dies, n_items))
    side = int(n_dies ** 0.5) + 1
    data_rows = []
    for d in range(n_dies):
        is_fail = rng.random() < 0.05
        row = {"SERIAL": f"S{d:06d}", "SHOT": str(d // 4 + 1), "DUT": str(d % 4 + 1),
               "XPOS": str(d % side), "YPOS": str(d // side),
               "BIN": "2" if is_fail else "1", "FAILTNO": ""}
        if is_fail:
            fi = int(rng.integers(0, n_items))
            values[d, fi] = 2.5  # HILIM(1.5) 밖
            row["FAILTNO"] = str(fi + 1)
        for i, it in enumerate(items):
            row[it] = f"{values[d, i]:.6f}"
        data_rows.append(row)

    df = pd.DataFrame(meta_rows + data_rows, columns=META_COLUMNS + items)
    return df, items


def build_manifest(lot_id, items):
    return {
        "sources": [{"index": 0, "name": "loadtest", "file_name": "loadtest.parquet"}],
        "meta": {"product_type": "PMIC", "product": "LOADTEST",
                 "lot_id": lot_id, "file_name": "loadtest"},
        "client": {"user": "loadtest", "host": "loadtest", "domain": ""},
        "selected_items": items,
        "sheets": [],
        "options": {},
        "mode": "Normal",
    }


# ── stdlib HTTP ──────────────────────────────────────────────────────────────

def encode_multipart(fields, files):
    boundary = f"----loadtest{uuid.uuid4().hex}"
    buf = io.BytesIO()
    for name, value in fields.items():
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; "
                  f"name=\"{name}\"\r\n\r\n{value}\r\n".encode())
    for name, filename, data, ctype in files:
        buf.write(f"--{boundary}\r\nContent-Disposition: form-data; "
                  f"name=\"{name}\"; filename=\"{filename}\"\r\n"
                  f"Content-Type: {ctype}\r\n\r\n".encode())
        buf.write(data)
        buf.write(b"\r\n")
    buf.write(f"--{boundary}--\r\n".encode())
    return buf.getvalue(), f"multipart/form-data; boundary={boundary}"


def http_get(url, timeout=120):
    req = urllib.request.Request(url, headers={"Accept-Encoding": "gzip"})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            body = res.read()
            return time.perf_counter() - t0, res.status, body
    except urllib.error.HTTPError as e:
        e.read()
        return time.perf_counter() - t0, e.code, b""
    except Exception:
        return time.perf_counter() - t0, 0, b""


def http_post_multipart(url, fields, files, timeout=300):
    body, ctype = encode_multipart(fields, files)
    req = urllib.request.Request(url, data=body, headers={"Content-Type": ctype})
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = res.read()
            return time.perf_counter() - t0, res.status, data
    except urllib.error.HTTPError as e:
        detail = e.read()[:200]
        print(f"  [upload] HTTP {e.code}: {detail}", file=sys.stderr)
        return time.perf_counter() - t0, e.code, b""
    except Exception as exc:
        print(f"  [upload] error: {exc}", file=sys.stderr)
        return time.perf_counter() - t0, 0, b""


# ── 시나리오 스레드 ──────────────────────────────────────────────────────────

class Ctx:
    def __init__(self, base, deadline):
        self.base = base
        self.deadline = deadline
        self.lock = threading.Lock()
        self.records = []          # (group, sec, status, ts)
        self.sessions = []         # 생성된 session_id
        self.stop = False

    def record(self, group, sec, status):
        with self.lock:
            self.records.append((group, sec, status, time.time()))
        if sec >= SLOW_SEC:
            print(f"  !! SLOW {group} {sec:.1f}s (status {status})", flush=True)

    def add_session(self, sid):
        with self.lock:
            self.sessions.append(sid)

    def pick_session(self):
        with self.lock:
            return random.choice(self.sessions) if self.sessions else None

    def running(self):
        return not self.stop and time.time() < self.deadline


def uploader(ctx, parquet_bytes, items, run_tag, idx):
    n = 0
    while ctx.running():
        lot_id = f"LT{run_tag}_{idx}_{n:03d}"
        manifest = build_manifest(lot_id, items)
        sec, status, body = http_post_multipart(
            f"{ctx.base}/pe/report/upload_webreport",
            {"manifest": json.dumps(manifest)},
            [("webreport_0", "loadtest.parquet", parquet_bytes,
              "application/vnd.apache.parquet")])
        ctx.record("upload_webreport", sec, status)
        if status == 200 and body:
            try:
                sid = json.loads(body)["session_id"]
                ctx.add_session(sid)
                sec, status, _ = http_get(f"{ctx.base}/pe/report/session/{sid}/full")
                ctx.record("session_full(cold)", sec, status)
            except Exception:
                pass
        n += 1
        time.sleep(1.0)


def reader(ctx):
    while ctx.running():
        sid = ctx.pick_session()
        if not sid:
            time.sleep(0.5)
            continue
        sec, status, _ = http_get(f"{ctx.base}/pe/report/view/{sid}")
        ctx.record("view_html", sec, status)
        sec, status, _ = http_get(f"{ctx.base}/pe/report/session/{sid}/full")
        ctx.record("session_full", sec, status)
        time.sleep(random.uniform(0.3, 1.0))


def dist_fetcher(ctx):
    while ctx.running():
        sid = ctx.pick_session()
        if not sid:
            time.sleep(0.5)
            continue
        sec, status, _ = http_get(
            f"{ctx.base}/pe/report/session/{sid}/web_report/distribution")
        ctx.record("distribution", sec, status)
        time.sleep(random.uniform(0.5, 1.5))


def static_user(ctx):
    while ctx.running():
        sec, status, _ = http_get(f"{ctx.base}/pe/report/")
        ctx.record("index_html", sec, status)
        sec, status, _ = http_get(f"{ctx.base}/healthz")
        ctx.record("healthz", sec, status)
        time.sleep(0.5)


# ── 서버 프로세스 관찰 ───────────────────────────────────────────────────────

def find_server_pid(port):
    try:
        for c in psutil.net_connections("tcp"):
            if c.laddr and c.laddr.port == port and c.status == "LISTEN" and c.pid:
                return c.pid
    except psutil.AccessDenied:
        pass
    return None


def observer(ctx, proc, samples):
    ncpu = psutil.cpu_count()
    proc.cpu_percent(interval=None)  # priming
    while ctx.running():
        time.sleep(0.5)
        try:
            cpu = proc.cpu_percent(interval=None)  # 프로세스 CPU% (전 코어 합, 최대 ncpu*100)
            rss = proc.memory_info().rss
            sys_mem = psutil.virtual_memory().used
            samples.append((time.time(), cpu, rss, sys_mem))
        except psutil.NoSuchProcess:
            print("  [observer] 서버 프로세스 종료 감지", file=sys.stderr)
            return
    _ = ncpu


# ── 리포트 ───────────────────────────────────────────────────────────────────

def pctl(vals, p):
    if not vals:
        return 0.0
    vals = sorted(vals)
    return vals[min(len(vals) - 1, int(len(vals) * p))]


def report(ctx, samples, args):
    print("\n" + "=" * 78)
    print(f"부하테스트 결과 — {args.duration}s, items={args.items}, dies={args.dies}, "
          f"동시 10명 (업로드2/조회4/분포2/경량2)")
    print("=" * 78)
    groups = {}
    for g, sec, status, ts in ctx.records:
        groups.setdefault(g, []).append((sec, status))
    print(f"{'endpoint':24s} {'n':>5s} {'p50':>8s} {'p95':>8s} {'max':>8s} "
          f"{'err':>4s} {'>15s':>5s}")
    for g in sorted(groups):
        rows = groups[g]
        secs = [s for s, _ in rows]
        errs = sum(1 for _, st in rows if st != 200)
        slow = sum(1 for s in secs if s >= SLOW_SEC)
        mark = "  !! SLOW" if slow else ""
        print(f"{g:24s} {len(rows):5d} {pctl(secs, .50):7.2f}s {pctl(secs, .95):7.2f}s "
              f"{max(secs):7.2f}s {errs:4d} {slow:5d}{mark}")

    slow_list = [(g, sec, st, ts) for g, sec, st, ts in ctx.records if sec >= SLOW_SEC]
    if slow_list:
        print(f"\n15초 초과 요청 {len(slow_list)}건:")
        for g, sec, st, ts in slow_list:
            print(f"  !! {time.strftime('%H:%M:%S', time.localtime(ts))} "
                  f"{g} {sec:.1f}s (status {st})")
    else:
        print("\n15초 초과 요청 없음 — UX 기준(15s) 충족.")

    if samples:
        ncpu = psutil.cpu_count()
        peak_cpu = max(s[1] for s in samples)
        peak_rss = max(s[2] for s in samples)
        peak_mem = max(s[3] for s in samples)
        avg_cpu = statistics.mean(s[1] for s in samples)
        print(f"\n서버 프로세스 실측 (0.5s 폴링, {len(samples)} 샘플):")
        print(f"  CPU 피크 {peak_cpu:.0f}% (전 코어 합, {ncpu}코어={ncpu*100}% 만점 "
              f"→ 정규화 {peak_cpu/ncpu:.0f}%) / 평균 {avg_cpu:.0f}%")
        print(f"  RSS 피크 {peak_rss/2**30:.2f} GB / 시스템 RAM 피크 {peak_mem/2**30:.2f} GB")

    # 관리자 패널 metrics 교차 확인 (A 파트 정확성 검증 겸용)
    try:
        url = f"{ctx.base}/pe/admin-{args.admin_secret}/api/metrics/history?window=300"
        _, status, body = http_get(url)
        if status == 200:
            import gzip as _gz
            try:
                body = _gz.decompress(body)
            except Exception:
                pass
            m = json.loads(body)
            pk = m["peaks"]["w300"]
            print(f"\n관리자 패널 metrics (최근 5분): CPU 피크 {pk['cpu']}% · "
                  f"RSS 피크 {pk['rss']/2**30:.2f} GB · 동시요청 피크 {pk['inflight']} / "
                  f"{m['threads']} 스레드")
    except Exception as exc:
        print(f"\n관리자 패널 metrics 조회 실패: {exc}")

    print(f"\n생성 세션 {len(ctx.sessions)}개 — 관리자 패널 세션 탭에서 "
          f"product=LOADTEST 검색 후 일괄 삭제 권장:")
    for sid in ctx.sessions:
        print(f"  {sid}")


def main():
    ap = argparse.ArgumentParser(description="10인 동시 부하테스트")
    ap.add_argument("--base", default="http://127.0.0.1:8000")
    ap.add_argument("--duration", type=int, default=180)
    ap.add_argument("--items", type=int, default=500)
    ap.add_argument("--dies", type=int, default=1000)
    ap.add_argument("--pid", type=int, default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--admin-secret", default="pte")
    args = ap.parse_args()

    port = args.port or urllib.parse.urlparse(args.base).port or 80
    pid = args.pid or find_server_pid(port)
    if not pid:
        sys.exit(f"서버 PID 자동 탐지 실패 (port {port}) — --pid 로 지정하세요.")
    proc = psutil.Process(pid)
    print(f"서버 프로세스: pid={pid} ({' '.join(proc.cmdline()[:3])})")

    print(f"honeyform 합성 중… items={args.items}, dies={args.dies}")
    t0 = time.perf_counter()
    df, items = make_honeyform_df(args.items, args.dies)
    parquet_bytes = encode_honeyform_parquet(df)
    print(f"  parquet {len(parquet_bytes)/2**20:.1f} MB ({time.perf_counter()-t0:.1f}s)")

    run_tag = time.strftime("%m%d%H%M%S")
    ctx = Ctx(args.base.rstrip("/"), time.time() + args.duration)
    samples = []
    threads = [threading.Thread(target=observer, args=(ctx, proc, samples), daemon=True)]
    threads += [threading.Thread(target=uploader, args=(ctx, parquet_bytes, items, run_tag, i),
                                 daemon=True) for i in range(2)]
    threads += [threading.Thread(target=reader, args=(ctx,), daemon=True) for _ in range(4)]
    threads += [threading.Thread(target=dist_fetcher, args=(ctx,), daemon=True) for _ in range(2)]
    threads += [threading.Thread(target=static_user, args=(ctx,), daemon=True) for _ in range(2)]

    print(f"부하 시작 — {args.duration}s")
    for t in threads:
        t.start()
    try:
        while ctx.running():
            time.sleep(1)
    except KeyboardInterrupt:
        ctx.stop = True
        print("중단됨 — 부분 결과로 리포트 생성")
    for t in threads:
        t.join(timeout=30)

    report(ctx, samples, args)


if __name__ == "__main__":
    main()
