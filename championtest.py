import asyncio
import subprocess
import time
from pathlib import Path

from websockets.exceptions import ConnectionClosedError

from poke_env.ps_client.server_configuration import ServerConfiguration
from poke_env import AccountConfiguration

from trainer import ChampionAgent


# =====================================================
# CONFIG  -- set these and run
# =====================================================

# Battle format to use for every battle (e.g. "gen7ubers", "gen3ou")
BATTLE_FORMAT = "gen7ubers"

# How many battles your team plays against EACH test team
BATTLES_PER_TESTTEAM = 2000

# Text file containing YOUR single team, in Showdown export format.
SINGLE_TEAM_FILE = "kukuiimproved.txt"

# Text file containing your set of test teams, in the GA output format
# produced by algo.py (each team preceded by a "--- #N | win rate: ... ---"
# header line).
TEST_TEAMS_FILE = "alolatestteams.txt"

MAX_CONCURRENT_BATTLES = 8

# How many times to retry a matchup if the connection to the server drops
# (e.g. the local Showdown server crashes mid-run) before giving up on it.
MAX_RETRIES_PER_MATCHUP = 5
RETRY_BACKOFF_SECONDS = 5

# poke_env's listen loop swallows websocket errors internally and never
# re-raises them, so a dead connection doesn't fail battle_against -- it just
# hangs forever waiting for events that will never arrive. If no battle
# finishes for this long, we treat the connection as dead and retry.
STALL_TIMEOUT_SECONDS = 90
STALL_POLL_INTERVAL_SECONDS = 2


class MatchupStalled(Exception):
    pass


# =====================================================
# SERVER CONFIG (LOCAL)
# =====================================================
LocalServerConfiguration = ServerConfiguration(
    "ws://127.0.0.1:8000/showdown/websocket",
    "http://127.0.0.1:8000/action.php",
)

SHOWDOWN_DIR = Path(__file__).parent / "pokemon-showdown"
SERVER_HOST = "127.0.0.1"
SERVER_PORT = 8000
SERVER_START_TIMEOUT = 180  # seconds to wait for the server to come back up (cold start transpiles TS and can take over a minute)

_server_process: subprocess.Popen | None = None
_server_lock = asyncio.Lock()


async def _is_port_open(host, port, timeout=1.0):
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


def _kill_anything_listening_on_port(port):
    """Windows-only: kill whatever process is already bound to `port`, so a
    crashed-but-still-running node process doesn't block a fresh restart with
    EADDRINUSE."""
    try:
        output = subprocess.check_output(
            ["netstat", "-ano"], text=True, stderr=subprocess.DEVNULL
        )
    except (OSError, subprocess.CalledProcessError):
        return
    pids = {
        line.split()[-1]
        for line in output.splitlines()
        if f":{port} " in line and "LISTENING" in line
    }
    for pid in pids:
        subprocess.run(
            ["taskkill", "/F", "/PID", pid],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


async def ensure_server_running():
    """Make sure the local Showdown server is reachable, restarting it if
    it's down. Safe to call from multiple concurrent matchups: the lock
    ensures only one restart happens even if several detect the outage at
    once."""
    global _server_process
    async with _server_lock:
        if await _is_port_open(SERVER_HOST, SERVER_PORT):
            return

        print("Local Showdown server is unreachable -- restarting it...")
        if _server_process is not None and _server_process.poll() is None:
            _server_process.kill()
            _server_process.wait(timeout=10)
        _kill_anything_listening_on_port(SERVER_PORT)

        _server_process = subprocess.Popen(
            ["node", "pokemon-showdown", "start"],
            cwd=str(SHOWDOWN_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        deadline = time.monotonic() + SERVER_START_TIMEOUT
        while time.monotonic() < deadline:
            if await _is_port_open(SERVER_HOST, SERVER_PORT):
                # Give it a moment to finish initializing past the bare TCP accept.
                await asyncio.sleep(2)
                print("Local Showdown server is back up.")
                return
            await asyncio.sleep(1)

        raise RuntimeError("Local Showdown server did not come back up in time")


# =====================================================
# LOADING TEAMS
# =====================================================
def load_single_team(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read().strip()


def load_test_teams(path):
    """Parses test teams out of a file in the GA output format produced by
    algo.py, where each team is preceded by a header line like:
        --- #1 | win rate: 1.000 (8/8) ---
    """
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    teams = []
    current = []
    for line in lines:
        if line.startswith("---") and line.rstrip().endswith("---"):
            if current:
                teams.append("\n".join(current).strip())
            current = []
        else:
            current.append(line)
    if current:
        teams.append("\n".join(current).strip())

    return [t for t in teams if t]


# =====================================================
# PLAYER CLASS
# =====================================================
class TestPlayer(ChampionAgent):
    pass


async def _battle_against_with_watchdog(my_player, opponent, n_battles):
    """Like my_player.battle_against, but bails out with MatchupStalled if no
    battle finishes for STALL_TIMEOUT_SECONDS, since a dropped connection
    otherwise just hangs forever instead of raising."""
    battle_task = asyncio.create_task(
        my_player.battle_against(opponent, n_battles=n_battles)
    )
    last_finished = my_player.n_finished_battles
    last_progress = time.monotonic()

    while not battle_task.done():
        await asyncio.sleep(STALL_POLL_INTERVAL_SECONDS)
        finished = my_player.n_finished_battles
        if finished != last_finished:
            last_finished = finished
            last_progress = time.monotonic()
        elif time.monotonic() - last_progress > STALL_TIMEOUT_SECONDS:
            battle_task.cancel()
            try:
                await battle_task
            except (asyncio.CancelledError, Exception):
                pass
            raise MatchupStalled(
                f"No battle finished in {STALL_TIMEOUT_SECONDS}s -- connection likely dead"
            )

    battle_task.result()


# =====================================================
# MAIN
# =====================================================
async def run_matchup(idx, single_team, test_team):
    remaining = BATTLES_PER_TESTTEAM
    total_wins = 0
    attempt = 0

    while remaining > 0:
        attempt += 1
        if attempt > MAX_RETRIES_PER_MATCHUP:
            raise RuntimeError(
                f"Matchup #{idx} failed after {MAX_RETRIES_PER_MATCHUP} attempts "
                f"({remaining}/{BATTLES_PER_TESTTEAM} battles still unplayed)"
            )

        await ensure_server_running()

        my_player = TestPlayer(
            account_configuration=AccountConfiguration.generate(f"MYTEAM_{idx}", rand=True),
            battle_format=BATTLE_FORMAT,
            server_configuration=LocalServerConfiguration,
            max_concurrent_battles=MAX_CONCURRENT_BATTLES,
            team=single_team,
        )
        opponent = TestPlayer(
            account_configuration=AccountConfiguration.generate(f"TESTTEAM_{idx}", rand=True),
            battle_format=BATTLE_FORMAT,
            server_configuration=LocalServerConfiguration,
            max_concurrent_battles=MAX_CONCURRENT_BATTLES,
            team=test_team,
        )

        try:
            print(
                f"=== Battling test team #{idx}: {remaining} battles "
                f"(attempt {attempt}) ==="
            )
            await _battle_against_with_watchdog(my_player, opponent, remaining)
            total_wins += my_player.n_won_battles
            remaining -= my_player.n_finished_battles
        except (ConnectionClosedError, OSError, MatchupStalled) as e:
            finished = my_player.n_finished_battles
            total_wins += my_player.n_won_battles
            remaining -= finished
            print(
                f"[matchup #{idx}] connection dropped after {finished} battles "
                f"({remaining} remaining): {e!r}. Retrying in "
                f"{RETRY_BACKOFF_SECONDS}s..."
            )
            await asyncio.sleep(RETRY_BACKOFF_SECONDS)
        finally:
            for player in (my_player, opponent):
                try:
                    await player.ps_client.stop_listening()
                except Exception:
                    pass

    return idx, total_wins, BATTLES_PER_TESTTEAM


async def main():
    single_team = load_single_team(SINGLE_TEAM_FILE)
    test_teams = load_test_teams(TEST_TEAMS_FILE)

    if not test_teams:
        raise ValueError(f"{TEST_TEAMS_FILE} contained no test teams")

    await ensure_server_running()

    results = await asyncio.gather(
        *(
            run_matchup(idx, single_team, test_team)
            for idx, test_team in enumerate(test_teams, start=1)
        )
    )

    print("\n=== Results ===\n")
    for idx, wins, total in sorted(results):
        win_rate = wins / total if total else 0.0
        print(f"Test team #{idx}: {wins}/{total} wins  ({win_rate:.3f} win rate)")


if __name__ == "__main__":
    asyncio.run(main())
