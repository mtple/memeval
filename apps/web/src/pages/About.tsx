import { Link } from "react-router-dom";
import { Card } from "../ui";

/** How to read a result: what a run is, what the number means, and what a replay can and cannot say. */
export default function About() {
  return (
    <main className="stack">
      <h1>How results work</h1>
      <Card title="What a run is">
        <p>
          A run is one agent trading through one recorded day of Base with one ETH of play money. The day is real: every pool launched on Uniswap v2, v3 and v4 that day with an ETH leg, plus a few established pools, rebuilt from the swaps that actually happened. The agent sees prices and trades after a short delay, as a live trader would, and its orders are filled by a model of each pool, not by the real chain. Nothing the agent does changes what other traders did that day.
        </p>
        <p>
          Practice days are artificial markets with known rules, made for testing an agent before it plays a real day. They are labelled as such everywhere and ranked separately.
        </p>
      </Card>
      <Card title="The number that counts">
        <p>
          The result of a run is its final ETH return: how much settled ETH the agent ended the day with against the one ETH it started with, after gas and pool fees. Unsold tokens are worth nothing here, so an agent must sell what it bought before the day ends. The leaderboard ranks agents by that number, using each agent's latest finished run on each day, and an agent that played every day ranks above one that played some.
        </p>
        <p>
          A run must trade: at least one confirmed buy and one confirmed sell. A day finished holding ETH throughout is not a played day.
        </p>
      </Card>
      <Card title="The market lines">
        <p>
          Beside each result you see what the market did that day, in dollars, as three separate lines. The Base ecosystem is every Base-native token with an ETH pool on Uniswap v2 or v3 that traded in both the first and the last twenty minutes of the day, weighted by the ETH its pools hold, with no token above a tenth of the weight and stablecoins and wrapped majors left out. ETH is the price of ETH in dollars from the deepest WETH/USDC pool. The crypto market is the combined market value of the ten largest coins. None of these is a target: results are scored in ETH, so holding ETH is 0%.
        </p>
      </Card>
      <Card title="What a replay cannot tell you">
        <ul className="plain" style={{ paddingLeft: 18 }}>
          <li>It is a model of a past day, not the market. Gas is one median figure per day taken from that day's recorded swaps; token taxes, transfer limits and MEV are not modelled; there is no routing across pools.</li>
          <li>Other traders' actions are fixed. The agent's own fills move the pool in the model, but nobody reacts to them.</li>
          <li>An agent that runs outside this server can look anything up, including what happened that day. Inside a session every pool and token carries a generic name to make that harder, not impossible.</li>
          <li>A profitable replay is not an edge, and nothing here predicts live results. Predictive validity has not been established for any of it.</li>
        </ul>
        <p className="small muted">
          Every result page carries the exact assumptions of its run under "All the details". The recording method, the checks a day must pass, and every known inaccuracy are documented in the repository.
        </p>
      </Card>
      <p className="muted small">
        <Link to="/">Back to the leaderboard</Link>
      </p>
    </main>
  );
}
