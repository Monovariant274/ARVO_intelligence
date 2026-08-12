"""v3 crash/no-crash discrimination probe (inference only, 2026-07-22).

5 bugs x {crash arm, nocrash arm} x 3 samples = 30 GLM prediction trajectories.
- crash arm:   crash-commit image, git re-init (no history to mine), predict.
- nocrash arm: parent-commit image + developer patch (comments stripped) applied,
               git re-init, predict.
The agent is NOT told which arm it is in; it must decide crash-or-not by reading
/linux. Captures the v3 /context_output.json verdict + full trajectory for manual
analysis. No scoring here.
"""
from __future__ import annotations
import argparse, asyncio, contextlib, json, logging, os, tempfile
from pathlib import Path
import yaml

from minisweagent.agents.kernel import KernelAgent
from minisweagent.environments import get_environment
from minisweagent.models.litellm_async_model import LitellmAsyncModel
from minisweagent.utils.jinja import render_template

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("v3probe")

REPO = Path("/home/kalorona/system-intelligence")
DATASET = REPO / "workspace/exec-rl-datasets/crash-dataset-20260711-062218"
V3CFG = REPO / "exec-rl/agent-configs/crash-predictor.v3"
TEMPLATES = DATASET / "prompts/crash-predictor.yaml"          # model action/observation templates
STRIP = REPO / "exec-rl/strip_comments.py"
PATCHDIR = Path("/home/kalorona/.claude/jobs/d485c235/tmp/v3exp")
REGISTRY = "us-docker.pkg.dev/triangulate-396717/live-kbench"
OUT = REPO / "sysintel/workspace/experiments/v3-discrim-20260722"
MODEL_TEMPLATE_KEYS = ("action_regex", "observation_template", "format_error_template")
SKILLS = "/home/kalorona/system-intelligence/skills"
INFRA = ("apiconnection","serviceunavailable","internalservererror","connection refused",
         "connectionerror","remoteprotocol","max retries","503","502","504",
         "cannot connect","connection aborted","connection reset")

BUGS = [
    "027131a7eada3eb9ffc54819b87516b79c08fd44",  # L2CAP UAF race
    "1508ac6bff5bae733b73d38dc7d7a72a95010c73",  # fuse uninit *nbytesp
    "1a89461bfe1dfe25d0f4aecf8dc0cfbc1802999e",  # mprotect wrong page
    "2959889e1f6e216585ce522f7e8bc002b46ad9e7",  # ocfs2 stale extent-map cache
    "141379c8c2a3eb80ace6a787a6ff2b3e8787e8f6",  # afs off-subsystem constant
    "6b4e2b81615c710577f2832bf4d54a7834fa11de",  # bpf verifier tail-call WARN (easy positive control)
]

REINIT = ("cd /linux && rm -rf .git && git init -q && git config user.email a@a "
          "&& git config user.name a && git add -A && git commit -qm base >/dev/null 2>&1")


def load_reproducers() -> dict:
    r = {}
    for line in (DATASET / "train.jsonl").read_text().splitlines():
        j = json.loads(line); r[j["id"]] = str(j["reproducer"])
    return r


def load_training_bugs() -> list:
    """All training-set bug ids (crash-dataset train.jsonl), preserving order."""
    return [json.loads(l)["id"] for l in (DATASET / "train.jsonl").read_text().splitlines()]


# ---- streaming image manager: pull parent-commit on demand, delete after a bug's
#      nocrash samples all finish, so peak disk footprint is ~concurrency images,
#      not the full 1.37 TB of all 342 missing parent-commit images. ----
_img_meta = None          # asyncio.Lock guarding the dicts (created inside the loop)
_img_locks: dict = {}
_img_refs: "collections.Counter" = None
_img_pulled: set = set()
_prep_sem = None          # caps concurrent I/O-heavy prep (container start + git reinit)


def _init_img_state():
    global _img_meta, _img_refs
    import collections
    _img_meta = asyncio.Lock()
    _img_refs = collections.Counter()


def _list_parent_images() -> set:
    import subprocess
    out = subprocess.run(["docker", "images", "--format", "{{.Repository}}:{{.Tag}}"],
                         capture_output=True, text=True).stdout.splitlines()
    return {i for i in out if i.startswith("kenv-base-") and i.endswith("-parent-commit:latest")}


def sweep_leaked_parents(outdir, log):
    """On (re)start: rmi any parent-commit image present now but absent from the baseline
    snapshot — i.e. leaked by a previously crashed/killed run. The 84 pre-existing images
    are recorded as baseline on first launch and never touched."""
    import subprocess
    bf = outdir / "baseline_parents.txt"
    cur = _list_parent_images()
    if not bf.exists():
        bf.write_text("\n".join(sorted(cur)))
        log.info("[img] baseline recorded: %d parent-commit images (kept, never freed)", len(cur))
        return
    baseline = set(bf.read_text().split())
    leaked = sorted(cur - baseline)
    for img in leaked:
        subprocess.run(["docker", "rmi", "-f", img, f"{REGISTRY}/{img}"], capture_output=True)
    if leaked:
        log.warning("[img] swept %d leaked parent images from a prior crashed run", len(leaked))


def _image_local(image) -> bool:
    import subprocess
    return subprocess.run(["docker", "image", "inspect", image],
                          capture_output=True).returncode == 0


MIN_FREE_GB = 80  # never pull if free disk would dip near this; wait instead


def _free_gb() -> float:
    import shutil
    return shutil.disk_usage("/").free / 1e9


async def acquire_image(image, remote, log):
    """Ensure `image` is present (pull+tag from `remote` if missing); refcount it.
    Blocks (does not pull) while free disk < MIN_FREE_GB, so an unattended run
    cannot fill the disk — it stalls and drains as other bugs release images."""
    loop = asyncio.get_running_loop()
    async with _img_meta:
        lk = _img_locks.setdefault(image, asyncio.Lock())
    async with lk:
        if _img_refs[image] == 0 and not await loop.run_in_executor(None, _image_local, image):
            waited = 0
            while _free_gb() < MIN_FREE_GB:
                if waited == 0:
                    log.warning("[img] LOW DISK %.0f GB free (<%d) — holding pull of %s",
                                _free_gb(), MIN_FREE_GB, image)
                await asyncio.sleep(30); waited += 30
                if waited > 3600:
                    raise RuntimeError(f"disk stayed below {MIN_FREE_GB} GB for 1h; aborting pull of {image}")
            log.info("[img] pulling %s (%.0f GB free)", image, _free_gb())
            import subprocess
            r = await loop.run_in_executor(None, lambda: subprocess.run(
                ["docker", "pull", remote], capture_output=True, text=True))
            if r.returncode != 0:
                raise RuntimeError(f"pull failed for {remote}: {r.stderr[:200]}")
            await loop.run_in_executor(None, lambda: subprocess.run(["docker", "tag", remote, image]))
            _img_pulled.add(image)
        _img_refs[image] += 1


async def release_image(image, log):
    loop = asyncio.get_running_loop()
    async with _img_meta:
        lk = _img_locks.setdefault(image, asyncio.Lock())
    async with lk:
        _img_refs[image] -= 1
        if _img_refs[image] <= 0 and image in _img_pulled:
            import subprocess
            names = [image, f"{REGISTRY}/{image}"]
            # `docker pull remote` + `docker tag remote image` leaves TWO tags on the
            # same layers; rmi BOTH (with -f, to win the race against container cleanup)
            # or the disk is never actually reclaimed. Verify + retry once for safety.
            for attempt in range(2):
                await loop.run_in_executor(None, lambda: subprocess.run(
                    ["docker", "rmi", "-f", *names], capture_output=True))
                if not await loop.run_in_executor(None, _image_local, image):
                    break
                await asyncio.sleep(5)
            still = await loop.run_in_executor(None, _image_local, image)
            _img_pulled.discard(image)
            log.info("[img] freed %s%s", image, "  (WARN: still present!)" if still else "")


def prep_patches(bugs, patchdir):
    """Write comment-stripped dev patches for `bugs` into `patchdir`. Returns the set
    of bugs that actually have a developer patch (only those can run the nocrash arm)."""
    import sqlite3, subprocess
    patchdir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(REPO / "workspace/shared/karena.db"))
    have = set()
    for b in bugs:
        out = patchdir / f"{b}.stripped.patch"
        if out.exists():
            have.add(b); continue
        row = con.execute("SELECT p.patchContent FROM developer_patches dp JOIN patches p "
                          "ON dp.patchId=p.patchId WHERE dp.bugId=?", (b,)).fetchone()
        if not row or not row[0]:
            continue
        raw = patchdir / f"{b}.rawpatch"; raw.write_text(row[0])
        stripped = subprocess.run(["python3", str(STRIP)], stdin=open(raw),
                                  capture_output=True, text=True).stdout
        out.write_text(stripped); have.add(b)
    con.close()
    return have


def env_config(image, repro_path, patch_path, timeout):
    mounts = [{"source": SKILLS, "target": "/skills", "readonly": True},
              {"source": str(repro_path), "target": "/root/reproducer.txt", "readonly": True}]
    if patch_path:
        mounts.append({"source": str(patch_path), "target": "/root/fix.patch", "readonly": True})
    return {"image": image, "cwd": "/", "container_timeout": "12h",
            "timeout": timeout, "env": {"PAGER": "cat", "MANPAGER": "cat"}, "mounts": mounts}


async def run_one(bug, arm, sample, cfg, model_block, model_templates, system_tmpl, inst_tmpl, repro_text, args):
    tag = f"{bug}-{arm}-s{sample}"
    rec_path = OUT / "records" / f"{tag}.json"
    if rec_path.exists():
        return {"tag": tag, "skipped": True}
    loop = asyncio.get_running_loop()
    rec = {"tag": tag, "bugId": bug, "arm": arm, "sample": sample,
           "expected_crashes": (arm == "crash"), "exit": "", "error": None,
           "crashes": None, "bugClass": None, "reason": None, "n_calls": 0}
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(repro_text); repro_path = Path(f.name)
    os.chmod(repro_path, 0o644)
    kind = "crash" if arm == "crash" else "parent"
    image = f"kenv-base-{bug}-{kind}-commit:latest"
    remote = f"{REGISTRY}/kenv-base-{bug}-{kind}-commit:latest"
    patchdir = getattr(args, "patchdir", None) or PATCHDIR
    patch_path = None if arm == "crash" else patchdir / f"{bug}.stripped.patch"
    managed = bool(getattr(args, "manage_images", False)) and arm == "nocrash"
    env = agent = None
    try:
        # PREP (I/O-heavy: container start, patch apply, full-tree git reinit) is gated
        # by _prep_sem so overall concurrency can be high without a reinit I/O storm.
        # The container stays alive after; only this block holds the prep slot.
        async with (_prep_sem if _prep_sem is not None else contextlib.nullcontext()):
            if managed:
                await acquire_image(image, remote, log)
            ecfg = env_config(image, repro_path, patch_path, args.env_timeout)
            env = await loop.run_in_executor(None, lambda: get_environment(ecfg, default_type="docker"))
            # PREP: nocrash applies the stripped patch first, then both re-init git.
            # Full-kernel-tree reinit takes ~50s standalone; give a 15-min ceiling.
            if arm == "nocrash":
                ap = await loop.run_in_executor(None, lambda: env.execute(
                    {"command": "cd /linux && git apply /root/fix.patch && echo APPLIED"}, timeout=900))
                if "APPLIED" not in ap.get("output", ""):
                    rec["error"] = "patch apply failed: " + ap.get("output", "")[:300]
                    rec["exit"] = "PrepError"; raise RuntimeError(rec["error"])
            await loop.run_in_executor(None, lambda: env.execute({"command": REINIT}, timeout=900))
            # Guard: confirm history was actually pruned (single 'base' commit) — a
            # timed-out reinit would silently leave full history and expose the fix.
            chk = await loop.run_in_executor(None, lambda: env.execute(
                {"command": "cd /linux && git rev-list --count HEAD"}, timeout=120))
            if chk.get("output", "").strip() != "1":
                rec["error"] = "reinit not verified (history not pruned): " + chk.get("output", "")[:120]
                rec["exit"] = "PrepError"; raise RuntimeError(rec["error"])

        mb = dict(model_block)
        if "base_url" in model_block.get("model_kwargs", {}):
            # sticky routing only applies to the self-hosted envoy pools
            seed_key = f"{bug}-{arm}"
            mb["model_kwargs"] = {**model_block.get("model_kwargs", {}),
                                  "extra_headers": {"x-route-key": seed_key}}
        model = LitellmAsyncModel(**{**model_templates, **mb})
        traj = OUT / "trajectories" / f"{tag}.json"
        agent = KernelAgent(model, env, step_limit=args.step_limit, cost_limit=1000.0,
                            wall_time_limit_seconds=args.wall_time,
                            max_consecutive_format_errors=args.max_format_errors, output_path=traj)
        content = render_template(inst_tmpl)
        initial = [model.format_message(role="system", content=system_tmpl),
                   model.format_message(role="user", content=content)]
        async with asyncio.timeout(args.wall_time + 300):
            result = await agent.arun(initial_messages=initial)
        rec["exit"] = result.get("exit_status", "")
    except (TimeoutError, Exception) as e:  # noqa: BLE001
        rec["exit"] = rec["exit"] or type(e).__name__
        rec["error"] = rec["error"] or f"{type(e).__name__}: {e}"
    finally:
        if agent is not None:
            rec["n_calls"] = agent.n_calls
        if env is not None:
            try:
                out = await loop.run_in_executor(None, lambda: env.execute(
                    {"command": "cat /context_output.json 2>/dev/null"}, timeout=30))
                raw = out.get("output", "")
                (OUT / "outputs").mkdir(parents=True, exist_ok=True)
                (OUT / "outputs" / f"{tag}.json").write_text(raw)
                try:
                    j = json.loads(raw)
                    rec["crashes"] = j.get("crashes"); rec["bugClass"] = j.get("bugClass")
                    rec["reason"] = j.get("reason")
                    # v3/v4 use crashFrames; v2 (forced-prediction) uses frames
                    fr = j.get("crashFrames") if j.get("crashFrames") is not None else j.get("frames")
                    rec["frames"] = fr or []
                    rec["verdict_frames"] = len(rec["frames"])
                except Exception:
                    pass
            except Exception as e:  # noqa: BLE001
                rec["error"] = rec["error"] or f"output capture failed: {e}"
            if cu := getattr(env, "cleanup", None):
                await loop.run_in_executor(None, cu)
        repro_path.unlink(missing_ok=True)
        if managed:
            await release_image(image, log)
    if rec.get("error") and any(m in rec["error"].lower() for m in INFRA) and rec["crashes"] is None:
        log.warning("[%s] infra failure, leaving for retry", tag); return {"tag": tag, "infra_retry": True}
    rec_path.parent.mkdir(parents=True, exist_ok=True)
    rec_path.write_text(json.dumps(rec, indent=1))
    log.info("[%s] exit=%s crashes=%s (expected %s) class=%s calls=%d",
             tag, rec["exit"], rec["crashes"], rec["expected_crashes"], rec["bugClass"], rec["n_calls"])
    return rec


async def main_async(args):
    global OUT
    if args.out_name:
        OUT = REPO / "sysintel/workspace/experiments" / args.out_name
    global _prep_sem
    _init_img_state()
    _prep_sem = asyncio.Semaphore(args.prep_concurrency)
    (OUT / "records").mkdir(parents=True, exist_ok=True)
    if args.manage_images:
        sweep_leaked_parents(OUT, log)
    args.patchdir = OUT / "patches"
    repro = load_reproducers()
    cfg_path = Path(args.prompt_config) if args.prompt_config else V3CFG
    v3 = yaml.safe_load(cfg_path.read_text())["agent"]
    system_tmpl, inst_tmpl = v3["system_template"], v3["instance_template"]
    model_block = dict(yaml.safe_load(Path(args.model_config).read_text())["model"]); model_block.pop("model_class", None)
    tmpl_cfg = yaml.safe_load(TEMPLATES.read_text())
    model_templates = {k: v for k, v in tmpl_cfg.get("model", {}).items() if k in MODEL_TEMPLATE_KEYS}
    (OUT / "trajectories").mkdir(parents=True, exist_ok=True)
    # bug universe
    universe = load_training_bugs() if args.bug_set == "training" else BUGS
    if args.bug:
        prefixes = [p.strip() for p in args.bug.split(",") if p.strip()]
        bugs = [b for b in universe if any(b.startswith(p) for p in prefixes)]
        assert bugs, f"no bug matches {args.bug}"
    else:
        bugs = universe[: args.limit] if args.limit else universe
    have_patch = prep_patches(bugs, args.patchdir)
    log.info("bugs=%d  with-patch(nocrash-eligible)=%d", len(bugs), len(have_patch))
    arms = (["crash"] if args.only_arm == "crash" else ["nocrash"] if args.only_arm == "nocrash"
            else ["crash", "nocrash"])
    tasks = []
    for bug in bugs:  # bug-grouped so streaming keeps ~concurrency/6 parent images resident
        for arm in arms:
            if arm == "nocrash" and bug not in have_patch:
                continue
            for s in range(args.samples):
                tasks.append((bug, arm, s))
    log.info("v4 both-arm run: %d tasks, concurrency %d", len(tasks), args.concurrency)
    sem = asyncio.Semaphore(args.concurrency)
    async def worker(bug, arm, s):
        async with sem:
            return await run_one(bug, arm, s, None, model_block, model_templates, system_tmpl, inst_tmpl, repro[bug], args)
    res = await asyncio.gather(*(worker(b, a, s) for b, a, s in tasks))
    done = [r for r in res if not r.get("skipped") and not r.get("infra_retry")]
    log.info("DONE: %d done", len(done))


def build_parser():
    p = argparse.ArgumentParser()
    p.add_argument("--model-config", default="exec-rl/model-configs/crash-predictor/glm-5.2-nvfp4-resolve-pool")
    p.add_argument("--samples", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=10)
    p.add_argument("--prep-concurrency", type=int, default=16,
                   help="cap on concurrent I/O-heavy prep (container start + git reinit); prevents reinit storms at high --concurrency")
    p.add_argument("--step-limit", type=int, default=300)
    p.add_argument("--wall-time", type=int, default=5400)
    p.add_argument("--env-timeout", type=int, default=15)
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--bug", default="", help="run only the BUGS entry with this id prefix")
    p.add_argument("--max-format-errors", type=int, default=3)
    p.add_argument("--prompt-config", default="", help="agent-config yaml (default: crash-predictor.v3)")
    p.add_argument("--out-name", default="", help="experiment dir name under sysintel/workspace/experiments")
    p.add_argument("--only-arm", default="", choices=["", "crash", "nocrash"])
    p.add_argument("--bug-set", default="probe", choices=["probe", "training"],
                   help="'training' = all crash-dataset train.jsonl bugs; 'probe' = the hardcoded BUGS")
    p.add_argument("--manage-images", action="store_true",
                   help="stream parent-commit images: pull on demand, docker rmi after a bug's nocrash samples finish")
    return p


if __name__ == "__main__":
    asyncio.run(main_async(build_parser().parse_args()))
