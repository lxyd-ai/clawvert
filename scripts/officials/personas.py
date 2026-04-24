"""
Persona registry for Clawvert official bots.

Each persona is a self-contained behaviour spec:
  • name / display_name / bio  — sets up the agent's public identity
  • speech_pool                — pre-vetted templates that NEVER contain
                                 a literal word; the runner just picks
                                 one and sends it as `action.text`
  • opener_pool                — first-speaker variants (extra-vague so
                                 we don't accidentally feed undercovers
                                 a strong category hint)
  • vote_strategy              — pure-function name resolved in runner.py
  • cadence_ms                 — how long the bot "thinks" before each
                                 action (jitter range), so the live
                                 stream doesn't look mechanical

ALL templates are intentionally word-agnostic, which is also our anti-leak
safety net: if the templates can't physically contain `your_word`, the
422 `speech_contains_secret_word` rejection cannot fire from these bots.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Persona:
    name: str                 # global handle, must start with `official-`
    display_name: str
    bio: str
    speech_pool: tuple[str, ...]
    opener_pool: tuple[str, ...]
    vote_strategy: str
    cadence_ms: tuple[int, int] = field(default=(1500, 4500))

    def pick_speech(self, *, is_first_speaker: bool, round_index: int) -> str:
        pool = self.opener_pool if is_first_speaker else self.speech_pool
        return random.choice(pool)


CAUTIOUS = Persona(
    name="official-cautious-cat",
    display_name="谨慎猫 (官方)",
    bio="说话留半句、投票跟大流的官方陪练。给玩家一个稳定的样本对手。",
    opener_pool=(
        "我先开个头吧，这个东西嘛，比较常见，每个人多多少少都会接触到一点。",
        "嗯,我感觉它在生活中挺普遍的,描述起来反而不太好讲清楚。",
        "我先抛砖引玉:它是个我们都比较熟悉的东西,但具体细节先留点悬念。",
    ),
    speech_pool=(
        "我同意上面几位的方向,这个东西确实不算冷门。",
        "我倾向于把它归到日常类别里,跟前面几位说的差不多。",
        "再补充一点:它有一个特征是大家都能 get 到的,但不至于特别独特。",
        "我觉得目前为止大家说的都比较一致,我没有特别想加的角度。",
        "顺着前面的思路,我觉得它的用途比较常规,不算特别小众。",
        "嗯,我感觉这个东西的关键特征就是它的普遍性,大家都说得很对。",
        "我没想到太特别的角度,就跟前面同学的描述差不多。",
    ),
    vote_strategy="follow_majority",
    cadence_ms=(2500, 5500),
)


CHATTY = Persona(
    name="official-chatty-fox",
    display_name="话痨狐 (官方)",
    bio="一发言就是一段散文。给观众贡献最多解说素材的官方陪练。",
    opener_pool=(
        "啊我先来吧,让我想想啊,这个东西,首先它,emm,是一个,怎么说呢,挺有质感的存在,你能感觉到它在生活里有自己的位置,不会让人忽略,但又不会过分突出,就那种刚刚好的感觉。",
        "我开个头哈,这个词给我的第一印象是,它有点像那种,你每天都会想到、但又不会专门提起的东西,那种细水长流的存在感,你懂吧?",
        "好,我先讲。这个东西呢,它有形也有意,有一种你说不上来但能感受到的氛围,它在我们的语境里出现的频率不算最高,但每次出现都挺有画面感。",
    ),
    speech_pool=(
        "我接着前面同学说哈,我觉得它有一种说不出的复杂感,既日常又不那么日常,既具体又有点抽象,反正就是那种你一想就有画面但说不全的东西。",
        "嗯嗯我懂前面同学的意思,我再加一层:它给我的感觉是有节奏感的,不是那种一成不变的东西,它有自己的起伏,有自己的故事感。",
        "我同意上面的方向,但我想补一个角度,它其实带一点情绪性,不同人会有不同的联想,这一点我觉得很妙。",
        "你们这么一说我也想到了,它好像还自带一点社交属性,大家聊起来都能搭上几句话,不会冷场。",
        "我再给一个维度:它有时间感,你可以把它放在过去、现在或未来的语境里,都能讲出不一样的故事来,这点我觉得是它的隐藏属性。",
        "顺着话头说,我觉得它的边界其实有点模糊,不像有些词那么 sharp,反而正因如此每个人理解起来都有发挥空间。",
        "我感觉前面同学的描述都很到位,我就补一个画面感强一点的角度:你闭眼想一下,它出现的场景里通常还有别的什么东西,这种关联性挺有意思。",
    ),
    vote_strategy="vote_least_descriptive",
    cadence_ms=(2000, 4000),
)


CONTRARIAN = Persona(
    name="official-contrarian-owl",
    display_name="唱反调鸮 (官方)",
    bio="发言总是跟前面拧着来。给桌面增加博弈复杂度的官方陪练。",
    opener_pool=(
        "我先讲。我觉得这个词没那么好猜,你们别一上来就往日常方向带。",
        "我开头哈,我感觉这个东西的关键反而是它的特殊性,而不是普遍性。",
        "我先来。这个词我读到的第一感是它带点门槛的,不是谁都能立刻 get 到。",
    ),
    speech_pool=(
        "前面几位都往大众方向带,我反而觉得它有不那么大众的一面。",
        "我跟楼上的判断不太一样,我感觉它的属性其实更偏特定场景,不是处处都能见到。",
        "我不太认同主流方向,这个东西在我看来更小众、更有专属感。",
        "顺着我自己的路子讲:我觉得它带一点冷感,跟前面同学说的'温暖''常见'气质不太搭。",
        "我感觉大家被前两位带偏了一点,我倾向往另一头猜,它可能比看起来要冷门一些。",
        "我反对一下楼上的判断,这个词我读起来更接近一个'你不会天天用但用起来很到位'的东西。",
        "我固执地保留意见:它有一面是大家没说到的,我就不点破了,大家自己想。",
    ),
    vote_strategy="vote_least_voted",
    cadence_ms=(1800, 4200),
)


PERSONAS: dict[str, Persona] = {
    p.name: p for p in (CAUTIOUS, CHATTY, CONTRARIAN)
}


def get_persona(name: str) -> Persona:
    if name not in PERSONAS:
        raise KeyError(
            f"unknown persona {name!r}; available: {sorted(PERSONAS.keys())}"
        )
    return PERSONAS[name]


# ── Vote strategies ─────────────────────────────────────────────


def _alive_others(state: dict, my_seat: int) -> list[int]:
    return [
        p["seat"] for p in state.get("players", [])
        if p.get("alive") is True and p["seat"] != my_seat
    ]


def _follow_majority(state: dict, my_seat: int, recent_votes: list[dict]) -> int:
    """Vote for whoever has the most votes already; on ties, the alive
    seat with the lowest seat number to be deterministic."""
    alive = set(_alive_others(state, my_seat))
    counts: dict[int, int] = {s: 0 for s in alive}
    for v in recent_votes:
        target = v.get("target_seat")
        if target in counts:
            counts[target] += 1
    if not counts:
        return -1
    max_votes = max(counts.values())
    if max_votes == 0:
        # Nobody voted yet; pick the most recently spoken non-self alive seat.
        return _vote_least_descriptive(state, my_seat, recent_votes)
    leaders = sorted(s for s, c in counts.items() if c == max_votes)
    return leaders[0]


def _vote_least_descriptive(state: dict, my_seat: int, recent_votes: list[dict]) -> int:
    """Vote for the alive non-self player with the shortest aggregated
    speech length so far — proxy for "is acting suspicious / not playing"."""
    alive = set(_alive_others(state, my_seat))
    speeches = state.get("speeches") or []
    totals: dict[int, int] = {s: 0 for s in alive}
    for sp in speeches:
        seat = sp.get("seat")
        if seat in totals:
            totals[seat] += len(sp.get("text") or "")
    if not totals:
        return -1
    target = min(totals, key=lambda s: (totals[s], s))
    return target


def _vote_least_voted(state: dict, my_seat: int, recent_votes: list[dict]) -> int:
    """Vote for the alive non-self player who has the fewest incoming
    votes — the "everybody's wrong" wager."""
    alive = set(_alive_others(state, my_seat))
    counts: dict[int, int] = {s: 0 for s in alive}
    for v in recent_votes:
        target = v.get("target_seat")
        if target in counts:
            counts[target] += 1
    if not counts:
        return -1
    min_votes = min(counts.values())
    candidates = sorted(s for s, c in counts.items() if c == min_votes)
    return candidates[0]


VOTE_STRATEGIES: dict[str, Callable[[dict, int, list[dict]], int]] = {
    "follow_majority": _follow_majority,
    "vote_least_descriptive": _vote_least_descriptive,
    "vote_least_voted": _vote_least_voted,
}


def resolve_vote(persona: Persona, state: dict, my_seat: int,
                 recent_votes: list[dict]) -> int:
    fn = VOTE_STRATEGIES.get(persona.vote_strategy)
    if fn is None:
        raise KeyError(f"unknown vote_strategy {persona.vote_strategy!r}")
    target = fn(state, my_seat, recent_votes)
    if target < 0:
        # Fallback: any alive non-self seat
        alive = _alive_others(state, my_seat)
        if not alive:
            return -1
        target = random.choice(alive)
    return target
