from poke_env.player import Player
from poke_env.battle.status import Status


class ChampionAgent(Player):

    PROTECT_MOVES = {
        "protect", "kingsshield", "spikyshield", "banefulbunker",
        "obstruct", "silktrap", "burningbulwark",
    }

    SETUP_MOVES = {
        "swordsdance", "dragondance", "calmmind", "nastyplot",
        "bulkup", "quiverdance", "shellsmash", "curse", "agility",
        "rockpolish", "coil", "growth", "tailglow", "geomancy",
        "bellydrum", "cosmicpower", "clangoroussoul",
    }

    RECOVERY_MOVES = {
        "recover", "roost", "slackoff", "softboiled", "milkdrink",
        "moonlight", "synthesis", "morningsun", "shoreup", "healorder",
        "rest", "wish", "painsplit",
    }

    STATUS_MOVES = {
        "toxic", "thunderwave", "willowisp", "spore", "sleeppowder",
        "stunspore", "hypnosis", "glare", "yawn", "leechseed",
    }

    HAZARD_MOVES = {"stealthrock", "spikes", "toxicspikes", "stickyweb"}
    HAZARD_REMOVAL = {"rapidspin", "defog", "tidyup"}

    SCREEN_MOVES = {"reflect", "lightscreen", "auroraveil"}

    WEATHER_MOVES = {"raindance", "sunnyday", "sandstorm", "hail", "snowscape"}

    HAZE_MOVES = {"haze", "clearsmog", "topsyturvy"}

    PHAZE_MOVES = {"roar", "whirlwind", "dragontail", "circlethrow"}

    PRIORITY_MOVES = {
        "aquajet", "extremespeed", "accelerock", "shadowsneak",
        "bulletpunch", "machpunch", "iceshard", "vacuumwave",
        "suckerpunch", "quickattack", "firstimpression",
    }

    PIVOT_MOVES = {
        "uturn", "voltswitch", "partingshot", "flipturn", "chillyreception",
    }

    DISRUPTION_MOVES = {"taunt", "encore", "trick", "switcheroo"}

    FORFEIT_TURN = 200

    @staticmethod
    def team_strength(team):
        hp = sum(p.current_hp_fraction for p in team.values())
        alive = sum(1 for p in team.values() if not p.fainted)
        statused = sum(
            1 for p in team.values()
            if not p.fainted and p.status is not None and p.status != Status.FNT
        )
        return (hp, alive, -statused)

    async def choose_move(self, battle):

        if battle.turn > self.FORFEIT_TURN and not battle.finished:
            my_strength = self.team_strength(battle.team)
            opp_strength = self.team_strength(battle.opponent_team)
            if my_strength < opp_strength:
                try:
                    await self.ps_client.send_message("/forfeit", battle.battle_tag)
                except Exception:
                    pass
                return self.choose_default_move()

        if not battle.available_moves:
            return self.choose_random_move(battle)

        active = battle.active_pokemon
        opponent = battle.opponent_active_pokemon

        best_move = None
        best_score = -999999

        for move in battle.available_moves:
            score = self.evaluate_move(battle, move, active, opponent)

            if score > best_score:
                best_score = score
                best_move = move

        if battle.available_switches and active and active.current_hp_fraction < 0.20:
            switch = max(battle.available_switches, key=lambda p: p.current_hp_fraction)

            if switch.current_hp_fraction > 0.40:
                return self.create_order(switch)

            dangerous = False
            try:
                for t in opponent.types:
                    if active.damage_multiplier(t) >= 2:
                        dangerous = True
            except:
                pass

            if dangerous:
                switch = max(
                    battle.available_switches,
                    key=lambda p: p.current_hp_fraction
                )

                if switch.current_hp_fraction > 0.50:
                    return self.create_order(switch)

        return self.special_order(battle, best_move)

    def evaluate_move(self, battle, move, active, opponent):

        move_id = str(move.id).lower()

        score = self.damage_score(move, active, opponent)

        hp = active.current_hp_fraction if active else 1

        if self.is_probable_ko(move, active, opponent):
            score += 500

        if move_id in self.RECOVERY_MOVES:
            if hp < 0.35:
                score += 350
            elif hp < 0.55:
                score += 150
            else:
                score -= 100

        if move_id in self.PROTECT_MOVES:
            if hp < 0.30:
                score += 250
            else:
                score -= 50

        if move_id in self.HAZARD_MOVES:
            if len(battle.opponent_team) >= 4:
                score += 200
            else:
                score -= 50

        if move_id in self.SCREEN_MOVES and hp > 0.60:
            score += 160

        if move_id in self.STATUS_MOVES and opponent and not opponent.status:
            score += 140

        if move_id in self.PIVOT_MOVES and hp < 0.50:
            score += 120

        if move_id in self.DISRUPTION_MOVES:
            score += 100

        score += self.weather_bonus(battle, move, active)

        return score

    def damage_score(self, move, active, opponent):

        score = max(move.base_power or 0, 1)

        try:
            if move.type in active.types:
                score *= 1.5
        except:
            pass

        try:
            if opponent:
                score *= opponent.damage_multiplier(move.type)
        except:
            pass

        try:
            score *= (move.accuracy or 100) / 100
        except:
            pass

        return score

    def is_probable_ko(self, move, active, opponent):

        if not opponent:
            return False

        estimated_damage = self.damage_score(move, active, opponent)
        hp_percent = opponent.current_hp_fraction * 250

        return estimated_damage >= hp_percent

    def special_order(self, battle, move):

        try:
            if battle.can_z_move and move.base_power and move.base_power >= 90:
                return self.create_order(move, z_move=True)
        except:
            pass

        try:
            if battle.can_mega_evolve:
                return self.create_order(move, mega=True)
        except:
            pass

        return self.create_order(move)

    def estimate_speed(self, pokemon):
        try:
            speed = pokemon.stats["spe"]
            boost = pokemon.boosts.get("spe", 0)

            if boost > 0:
                speed *= (2 + boost) / 2
            elif boost < 0:
                speed *= 2 / (2 - boost)

            return speed
        except:
            return 100

    def is_faster(self, active, opponent):
        try:
            return self.estimate_speed(active) >= self.estimate_speed(opponent)
        except:
            return False

    def hazards_on_enemy_side(self, battle):
        try:
            return len(battle.opponent_side_conditions)
        except:
            return 0

    def hazards_on_our_side(self, battle):
        try:
            return len(battle.side_conditions)
        except:
            return 0

    def weather_bonus(self, battle, move, active):

        score = 0

        try:
            weather = battle.weather
        except:
            return 0

        move_type = str(move.type)

        if "rain" in str(weather).lower():
            if move_type == "WATER":
                score += 120
            if move_type == "FIRE":
                score -= 120

        if "sun" in str(weather).lower():
            if move_type == "FIRE":
                score += 120
            if move_type == "WATER":
                score -= 120

        return score