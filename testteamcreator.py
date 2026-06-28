import asyncio
import random
from dataclasses import dataclass

from poke_env.ps_client.server_configuration import ServerConfiguration
from poke_env import AccountConfiguration

from trainer import ChampionAgent


# -----------------------------
# CONFIG  -- set these and run
# -----------------------------
BATTLE_FORMAT = "gen7ubers"        # e.g. "gen3ou", "gen3ubers"
INITIAL_POP_SIZE = 2048          # size of the first, all-random generation
POOL_FILE = "alola.txt"         # text file with your pool of pokemon sets
FINAL_POP_SIZE = 8              # how many teams you want left at the end (must be a multiple of 4)

TEAM_SIZE = 6
MAX_CONCURRENT_BATTLES = 8
BATTLES_PER_MATCHUP = 4          # battles played each time two teams are paired up
MUTATION_RATE = 0.5              # chance each slot in a team gets swapped on mutation
NUM_PARALLEL_MATCHUPS = 8        # how many matchups to evaluate at once, each using its own pair of players


# -----------------------------
# SERVER CONFIG (LOCAL)
# -----------------------------
LocalServerConfiguration = ServerConfiguration(
    "ws://localhost:8000/showdown/websocket",
    "http://localhost:8000/action.php",
)


# -----------------------------
# LOAD POOL
# -----------------------------
def load_pool(path):
    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()
    return [p.strip() for p in raw.split("\n\n") if p.strip()]


POOL = load_pool(POOL_FILE)

if len(POOL) < TEAM_SIZE:
    raise ValueError(f"{POOL_FILE} only has {len(POOL)} pokemon, need at least {TEAM_SIZE}")

if FINAL_POP_SIZE % 4 != 0 or FINAL_POP_SIZE < 4:
    raise ValueError("FINAL_POP_SIZE must be a multiple of 4 (>= 4)")

if INITIAL_POP_SIZE < FINAL_POP_SIZE or INITIAL_POP_SIZE % FINAL_POP_SIZE != 0:
    raise ValueError("INITIAL_POP_SIZE must be a multiple of FINAL_POP_SIZE")

_ratio = INITIAL_POP_SIZE // FINAL_POP_SIZE
GA_GENERATIONS = _ratio.bit_length() - 1
if 2 ** GA_GENERATIONS != _ratio:
    raise ValueError("INITIAL_POP_SIZE must be FINAL_POP_SIZE times a power of 2")


def random_team_str():
    return "\n\n".join(random.sample(POOL, TEAM_SIZE))


# -----------------------------
# INDIVIDUAL
# -----------------------------
@dataclass
class Individual:
    team: str
    wins: int = 0
    battles: int = 0

    @property
    def score(self):
        return self.wins / self.battles if self.battles else 0.0


# -----------------------------
# PLAYER CLASS
# -----------------------------
class EvalPlayer(ChampionAgent):
    pass


# -----------------------------
# EVALUATION
# -----------------------------
async def evaluate_population(population, player_pairs):
    for ind in population:
        ind.wins = 0
        ind.battles = 0

    indices = list(range(len(population)))
    random.shuffle(indices)
    matchup_queue = [(indices[i], indices[i + 1]) for i in range(0, len(indices) - 1, 2)]

    async def run_matchup(p1, p2, i, j):
        ind_a = population[i]
        ind_b = population[j]

        p1.update_team(ind_a.team)
        p2.update_team(ind_b.team)

        await p1.battle_against(p2, n_battles=BATTLES_PER_MATCHUP)

        ind_a.wins += p1.n_won_battles
        ind_a.battles += BATTLES_PER_MATCHUP
        ind_b.wins += p2.n_won_battles
        ind_b.battles += BATTLES_PER_MATCHUP

        p1.reset_battles()
        p2.reset_battles()

    async def worker(p1, p2):
        while matchup_queue:
            i, j = matchup_queue.pop()
            await run_matchup(p1, p2, i, j)

    await asyncio.gather(*(worker(p1, p2) for p1, p2 in player_pairs))


# -----------------------------
# GA OPERATORS
# -----------------------------
def team_signature(team: str):
    return frozenset(team.split("\n\n"))


def select_top(population, k):
    return sorted(population, key=lambda ind: ind.score, reverse=True)[:k]


def unique_top(population, k):
    seen = set()
    result = []
    for ind in sorted(population, key=lambda ind: ind.score, reverse=True):
        sig = team_signature(ind.team)
        if sig not in seen:
            seen.add(sig)
            result.append(ind)
        if len(result) == k:
            break
    return result, seen


def crossover(parent_a: Individual, parent_b: Individual) -> Individual:
    mons_a = parent_a.team.split("\n\n")
    mons_b = parent_b.team.split("\n\n")
    cut = random.randint(1, TEAM_SIZE - 1)
    child_mons = mons_a[:cut] + mons_b[cut:]

    seen = set()
    final = []
    for m in child_mons:
        if m not in seen:
            seen.add(m)
            final.append(m)

    candidates = [m for m in POOL if m not in seen]
    random.shuffle(candidates)
    while len(final) < TEAM_SIZE and candidates:
        mon = candidates.pop()
        final.append(mon)
        seen.add(mon)

    return Individual("\n\n".join(final[:TEAM_SIZE]))


def mutate(ind: Individual, rate=MUTATION_RATE) -> Individual:
    mons = ind.team.split("\n\n")
    for i in range(len(mons)):
        if random.random() < rate:
            choices = [m for m in POOL if m not in mons]
            if choices:
                mons[i] = random.choice(choices)
    return Individual("\n\n".join(mons))


def build_next_generation(population):
    group_size = len(population) // 8

    elites, seen = unique_top(population, group_size)

    def add_unique(make_fn, count, max_attempts_per_slot=50):
        result = []
        attempts = 0
        max_attempts = count * max_attempts_per_slot
        while len(result) < count and attempts < max_attempts:
            attempts += 1
            ind = make_fn()
            sig = team_signature(ind.team)
            if sig not in seen:
                seen.add(sig)
                result.append(ind)
        # if the pool is too small to keep finding fresh combos, give up
        # on uniqueness for the remaining slots rather than looping forever
        while len(result) < count:
            result.append(make_fn())
        return result

    mutation_group = add_unique(lambda: mutate(random.choice(elites)), group_size)
    crossover_group = add_unique(
        lambda: crossover(*random.choices(elites, k=2)), group_size
    )
    random_group = add_unique(lambda: Individual(random_team_str()), group_size)

    return [Individual(e.team) for e in elites] + mutation_group + crossover_group + random_group


# -----------------------------
# MAIN
# -----------------------------
async def main():
    player_pairs = []
    for n in range(NUM_PARALLEL_MATCHUPS):
        p1 = EvalPlayer(
            account_configuration=AccountConfiguration.generate(f"GA_EVAL_A{n}", rand=True),
            battle_format=BATTLE_FORMAT,
            server_configuration=LocalServerConfiguration,
            max_concurrent_battles=MAX_CONCURRENT_BATTLES,
        )
        p2 = EvalPlayer(
            account_configuration=AccountConfiguration.generate(f"GA_EVAL_B{n}", rand=True),
            battle_format=BATTLE_FORMAT,
            server_configuration=LocalServerConfiguration,
            max_concurrent_battles=MAX_CONCURRENT_BATTLES,
        )
        player_pairs.append((p1, p2))

    population = [Individual(random_team_str()) for _ in range(INITIAL_POP_SIZE)]

    for gen in range(GA_GENERATIONS):
        print(f"=== Generation {gen} ({len(population)} teams) ===")
        await evaluate_population(population, player_pairs)

        best = max(population, key=lambda ind: ind.score)
        avg = sum(ind.score for ind in population) / len(population)
        print(f"Best win rate: {best.score:.3f}  |  Avg win rate: {avg:.3f}")

        if gen == GA_GENERATIONS - 1:
            population, _ = unique_top(population, FINAL_POP_SIZE)
        else:
            population = build_next_generation(population)

    # Final evaluation of the surviving population
    print(f"=== Final evaluation ({len(population)} teams) ===")
    await evaluate_population(population, player_pairs)

    for p1, p2 in player_pairs:
        await p1.ps_client.stop_listening()
        await p2.ps_client.stop_listening()

    population.sort(key=lambda ind: ind.score, reverse=True)

    print("\n=== Final teams ===\n")
    with open("alolatestteams.txt", "w", encoding="utf-8") as f:
        for rank, ind in enumerate(population, start=1):
            header = f"--- #{rank} | win rate: {ind.score:.3f} ({ind.wins}/{ind.battles}) ---"
            print(header)
            print(ind.team)
            print()
            f.write(header + "\n")
            f.write(ind.team + "\n\n")


if __name__ == "__main__":
    asyncio.run(main())
