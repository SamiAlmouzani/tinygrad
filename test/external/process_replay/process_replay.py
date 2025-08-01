#!/usr/bin/env python3
# compare kernels created by HEAD against master
import os, multiprocessing, logging, pickle, sqlite3, difflib, warnings, itertools
from typing import Callable, Any
from tinygrad.helpers import VERSION, Context, ContextVar, colored, db_connection, getenv, tqdm
from tinygrad.kernelize.kernelize import get_kernelize_map
from tinygrad.renderer import Renderer, ProgramSpec
from tinygrad.engine.realize import get_program
from tinygrad.uop.ops import UOp, Ops, KernelInfo

# *** process replay settings

# internal
PAGE_SIZE = getenv("PAGE_SIZE", 100)
REF = os.getenv("GITHUB_REF_NAME", "")
MAX_DIFF_PCT = getenv("PROCESS_REPLAY_MAX_DIFF_PCT", 20)
TABLE_NAME = f"process_replay_{VERSION}"
os.environ["CAPTURE_PROCESS_REPLAY"] = "0"
early_stop = multiprocessing.Event()
logging.basicConfig(level=logging.INFO, format="%(message)s")
MAX_LINES = 500
def trunc_log(x):
  if len(lines:=repr(x).splitlines()) > MAX_LINES: lines = lines[:MAX_LINES]+[f"WARN: truncated string with {len(lines)} lines"]
  logging.info("\n".join(lines))

# user config
ASSERT_DIFF = int((flag:="[pr]") in os.getenv("COMMIT_MESSAGE", flag) or flag in os.getenv("PR_TITLE", flag))
if not getenv("ASSERT_PROCESS_REPLAY", 1): ASSERT_DIFF = 0
SKIP_PROCESS_REPLAY = (k:="[skip_process_replay]") in os.getenv("COMMIT_MESSAGE", "") or k in os.getenv("PR_TITLE", "")
if REF == "master": SKIP_PROCESS_REPLAY = True
class ProcessReplayWarning(Warning): pass

# *** replay the function and convert return values to string

def replay_kernelize(ret:dict[UOp, UOp], big_sink:UOp) -> tuple[str, str, tuple[Any, ...]]:
  UOp.unique_num = itertools.count(max([u.arg for u in big_sink.toposort() if u.op is Ops.UNIQUE], default=0)+1)
  new_sink = big_sink.substitute(get_kernelize_map(big_sink))
  def to_str(ret:UOp) -> str:
    asts = [repr(u.arg.ast) for u in ret.toposort() if u.op is Ops.KERNEL]
    return "\n".join([f"{len(asts)} kernels", *asts])
  return to_str(new_sink), to_str(ret[big_sink]), (big_sink,)

def replay_get_program(p:ProgramSpec, ast:UOp, renderer:Renderer) -> tuple[str, str, tuple[Any, ...]]:
  p2 = get_program(ast.replace(arg=KernelInfo(opts_to_apply=p.applied_opts, name=p.name)) if ast.arg is None else ast, renderer)
  def to_str(ret:ProgramSpec) -> str: return ret.src
  return to_str(p2), to_str(p), (p.ast, renderer, p.applied_opts)

replayers: dict[str, Callable[..., tuple[str, str, tuple[Any, ...]]]] = {"get_kernelize_map":replay_kernelize, "get_program":replay_get_program}

# *** run replayers on captured rows and print diffs

def diff(offset:int) -> None:
  if ASSERT_DIFF: warnings.filterwarnings("error", category=ProcessReplayWarning)
  if early_stop.is_set(): return None
  conn = db_connection()
  cur = conn.cursor()
  cur.execute(f"SELECT val FROM '{TABLE_NAME}' LIMIT ? OFFSET ?", (PAGE_SIZE, offset))
  changed = 0
  for row in cur.fetchall():
    if changed > MAX_DIFF_PCT:
      warnings.warn(f"detected changes in over {MAX_DIFF_PCT}%. skipping further diff generation.", ProcessReplayWarning)
      early_stop.set()
      break
    try:
      name, args, kwargs, ctx_vals, loc, ret = pickle.loads(row[0])
      ctx_vars = {k:v.value for k,v in ctx_vals.items() if k != "DEBUG" and (var:=ContextVar._cache.get(k)) is not None and var.value != v.value}
      if (replayer:=replayers.get(name)) is None: continue
      with Context(**ctx_vars): good, compare, metadata = replayer(ret, *args, **kwargs)
      if good != compare:
        for m in metadata: trunc_log(m)
        logging.info(loc)
        for line in difflib.unified_diff(good.splitlines(), compare.splitlines()):
          logging.info(colored(line, "red" if line.startswith("-") else "green" if line.startswith("+") else None))
        if ctx_vars: logging.info(ctx_vars)
        warnings.warn("PROCESS REPLAY DETECTED CHANGE", ProcessReplayWarning)
    except Exception as e:
      changed += 1
      warnings.warn(e, ProcessReplayWarning)
  conn.commit()
  cur.close()

# *** main loop

if __name__ == "__main__":
  if SKIP_PROCESS_REPLAY:
    logging.info("skipping process replay.")
    exit(0)

  conn = db_connection()
  cur = conn.cursor()
  try: row_count = cur.execute(f"select count(*) from '{TABLE_NAME}'").fetchone()[0]
  except sqlite3.OperationalError:
    warnings.warn(f"{TABLE_NAME} isn't accessible in master, did DB_VERSION change?", ProcessReplayWarning)
    exit(int(ASSERT_DIFF))
  finally:
    conn.commit()
    cur.close()

  logging.info(f"running process replay with {ASSERT_DIFF=}")
  with multiprocessing.get_context("spawn").Pool(multiprocessing.cpu_count()) as pool:
    inputs = list(range(0, row_count, PAGE_SIZE))
    list(tqdm(pool.imap_unordered(diff, inputs), total=len(inputs)))
    pool.close()
    pool.join()
    pool.terminate()
