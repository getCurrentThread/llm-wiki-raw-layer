"""아카이브 건수를 서버 total_msg_count 와 대조하되, `mattertok reconcile`(archive.py)과 달리
system_* 메시지를 실제로 걸러내고 delete_at>0 게시물 존재 여부까지 확인한다. 읽기 전용 —
manifest 도 posts/*.jsonl 도 쓰지 않는다. 라이브 수집기가 도는 중에도 안전하다.

`mattertok reconcile` 은 manifest.post_count(= system+regular 합계)를 그대로 expected 와
비교해서 "over" 로 뭉뚱그린다(REASONS 에 설명은 있지만 수치로 쪼개지 않는다). 이 스크립트는
잔여 delta 가 실제로 system 메시지로 설명되는지, 아니면 regular 메시지가 진짜로 부족한지를
채널별로 분리해서 보여준다 — kb-mt-open-countmismatch 의 08-14 진단(초과분=system, 부족분=
delete_at>0 삭제 게시물, 표준 API 로 복구 불가)을 재현 가능한 명령으로 남겨 둔 것.

    python mt_reconcile.py                 # 요약 + 부족 상위 채널
    python mt_reconcile.py --short-only     # 재수집 후보만, 설명 안 되는 부족 있으면 exit 1
    python mt_reconcile.py --json
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import asdict, dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "src"))

from mattertok.archive import Manifest, RECONCILE_FLOOR, RECONCILE_TOLERANCE  # noqa: E402
from mattertok.cli import bold, cyan, dim, green, pad, red, yellow  # noqa: E402

DEAD_CHANNEL_DAYS = 90  # 이보다 오래 전에 마지막 글이 달렸으면 '죽은 방' — 백필 지연으로 볼 수 없다


@dataclass
class Row:
    channel_id: str
    display: str
    type: str
    regular: int
    system: int
    deleted_on_disk: int  # delete_at>0 인데 아카이브에 있는 건 (있으면 안 됨 — API 가 안 줌)
    expected: int
    regular_drift: int
    verdict: str  # ok | over | short
    dead: bool
    backfill_done: bool
    truncated: bool


def count_posts(path: Path) -> tuple[int, int, int]:
    """(regular, system, delete_at>0 인데 디스크에 있는 건수)."""
    if not path.is_file():
        return 0, 0, 0
    regular = system = deleted = 0
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                post = json.loads(line)
            except ValueError:
                continue  # 쓰다 만 마지막 줄 — count_posts_on_disk 와 동일 취급
            if post.get("delete_at"):
                deleted += 1
            if str(post.get("type", "")).startswith("system_"):
                system += 1
            else:
                regular += 1
    return regular, system, deleted


def reconcile(manifest: Manifest, posts_dir: Path, *, tolerance: float) -> list[Row]:
    now_ms = int(time.time() * 1000)
    dead_cutoff = DEAD_CHANNEL_DAYS * 86400 * 1000
    rows = []
    for state in manifest.channels.values():
        regular, system, deleted = count_posts(posts_dir / state.path)
        drift = regular - state.expected
        allowed = max(RECONCILE_FLOOR, int(state.expected * tolerance))
        if state.expected <= 0:
            verdict = "ok"  # 비교 대상 없음 — 서버 카운트 모르는 채널까지 short 로 몰지 않는다
        elif abs(drift) <= allowed:
            verdict = "ok"
        elif drift > 0:
            verdict = "over"
        else:
            verdict = "short"
        rows.append(Row(
            channel_id=state.channel_id,
            display=state.display,
            type=state.type,
            regular=regular,
            system=system,
            deleted_on_disk=deleted,
            expected=state.expected,
            regular_drift=drift,
            verdict=verdict,
            dead=(now_ms - state.newest_create_at) > dead_cutoff if state.newest_create_at else False,
            backfill_done=state.backfill_done,
            truncated=state.truncated,
        ))
    rows.sort(key=lambda r: r.regular_drift)
    return rows


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--out", default=str(Path(__file__).parent / "archive"))
    ap.add_argument("--tolerance", type=float, default=RECONCILE_TOLERANCE * 100,
                     help="퍼센트 (기본 %(default)s)")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--short-only", action="store_true",
                     help="재수집 후보만 출력. dead+backfill_done+not truncated 로 설명되는 short 는 "
                          "'explained' 로 걸러내고, 나머지가 있으면 exit 1")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    out = Path(args.out).expanduser().resolve()
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        print(red(f"{manifest_path} 가 없습니다."), file=sys.stderr)
        return 2
    manifest = Manifest.load(manifest_path)
    rows = reconcile(manifest, out / "posts", tolerance=args.tolerance / 100)

    short = [r for r in rows if r.verdict == "short"]
    # dead 채널 + backfill 완료 + retention 절단 아님 = delete_at>0 삭제 게시물로 설명되는 부족.
    # backfill_done=True + truncated=False 면 archiver 가 oldest_cursor 를 끝까지 소진하고
    # newest 에도 도달했다는 뜻 — 양끝을 다 훑었는데 내부가 비었다는 것 자체가 '재수집으로는
    # 메워지지 않는 gap' 이라는 증거다(다시 돌려도 같은 두 끝에서 같은 결과가 나온다).
    # delete_at>0 인데 디스크에 있는 건 0건(위 deleted_on_disk_total) — 즉 /posts API 는
    # 삭제 게시물을 절대 주지 않는다는 전제가 이 아카이브 전체에서 실측으로 확인된다.
    # 그 둘을 합치면: 양끝 소진 + API 가 삭제분을 안 준다 = 남은 gap 은 delete_at>0 로만
    # 설명된다. `dead`(90일 넘게 조용한 방)는 참고용 신호일 뿐 필요조건이 아니다 — 활성
    # 채널도 똑같은 두 조건(양끝 소진)을 만족하면 동일하게 설명된다.
    explained = [r for r in short if r.backfill_done and not r.truncated]
    explained_ids = {r.channel_id for r in explained}
    unexplained = [r for r in short if r.channel_id not in explained_ids]

    if args.json:
        payload = {
            "channels": len(rows),
            "ok": sum(1 for r in rows if r.verdict == "ok"),
            "over": sum(1 for r in rows if r.verdict == "over"),
            "short": len(short),
            "short_explained_by_deletion": len(explained),
            "short_unexplained": len(unexplained),
            "regular_total": sum(r.regular for r in rows),
            "system_total": sum(r.system for r in rows),
            "deleted_on_disk_total": sum(r.deleted_on_disk for r in rows),
            "rows": [asdict(r) for r in (unexplained if args.short_only else rows)],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=1))
        return 0 if not unexplained else 1

    if args.short_only:
        print(bold(f"재수집 후보 (설명 안 되는 short) {len(unexplained)}개")
              + dim(f"  — short 총 {len(short)}개 중 {len(explained)}개는 delete_at>0 로 설명됨"))
        for r in unexplained[: args.top]:
            print(f"  {pad(r.type, 3)} {pad(r.display, 32)} "
                  f"{yellow(f'{r.regular_drift:+,}')}  regular={r.regular:,} expected={r.expected:,} "
                  f"backfill_done={r.backfill_done} truncated={r.truncated}")
        return 0 if not unexplained else 1

    print(bold("── regular-only 대조 (system_* 제외) ──"), dim(str(out)))
    print(f"  채널 {len(rows):,}개 · regular {sum(r.regular for r in rows):,}건 "
          f"· system {sum(r.system for r in rows):,}건 · 서버기준 {sum(r.expected for r in rows):,}건 "
          f"· delete_at>0(디스크상) {sum(r.deleted_on_disk for r in rows):,}건")
    print(f"  판정  ok {sum(1 for r in rows if r.verdict=='ok'):,}개 · "
          f"over {sum(1 for r in rows if r.verdict=='over'):,}개 · short {len(short):,}개 "
          f"({green(f'{len(explained)} 삭제분 설명됨')} / {red(f'{len(unexplained)} 미설명')})")

    if short:
        print(bold(f"\n  short 상위 {min(args.top, len(short))}개"))
        for r in short[: args.top]:
            tag = green("삭제분") if r.channel_id in explained_ids else red("미설명")
            print(f"  {pad(r.type, 3)} {pad(r.display, 32)} "
                  f"{r.regular_drift:+,}  [{tag}]  "
                  f"backfill_done={r.backfill_done} truncated={r.truncated} dead={r.dead}")
    return 0 if not unexplained else 1


if __name__ == "__main__":
    sys.exit(main())

