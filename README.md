# OnCall Memory

OnCall Memory is a lightweight incident-response assistant. It diagnoses pasted alerts and log snippets with Groq, then uses Hindsight as long-term memory so resolved incidents can inform future responses.

## Architecture

```text
                         +--------------------+
 Incident alert/logs --->| Streamlit interface |
                         +----------+---------+
                                    |
                     +--------------+--------------+
                     |                             |
              Memory OFF                    Memory ON
                     |                             |
                     |                    +--------v---------+
                     |                    | Hindsight recall |
                     |                    | + reflection     |
                     |                    +--------+---------+
                     |                             |
                     +--------------+--------------+
                                    v
                           +--------+--------+
                           | Groq diagnosis  |
                           | primary + retry |
                           | fallback model  |
                           +--------+--------+
                                    |
                         diagnosis and comparison
                                    |
                  resolved root cause + fix applied
                                    v
                           Hindsight retain
```

## How Hindsight memory is used

The app connects with `Hindsight(base_url=HINDSIGHT_URL)` and includes `HINDSIGHT_API_KEY` when configured. All incident memory is stored in the `oncall` bank.

- **retain** — “Mark resolved” saves the original alert/log snippet together with the root cause and fix as a postmortem. `seed.py` also retains 15 synthetic postmortems.
- **recall** — With memory enabled, the app searches for incidents relevant to the current alert and logs before producing the memory-enriched diagnosis. Memory OFF does not perform a recall.
- **reflect** — The app asks Hindsight for a reflection on the current incident and adds it to the recalled context passed to Groq.

Before any matching postmortem is retained, a diagnosis of a Redis timeout may only suggest checking connectivity and service health. After retaining a resolved incident that links `RedisTimeoutError` to clients pinned to an old primary after failover, the next similar incident can include that prior cause and the endpoint-refresh fix in its memory-enriched answer.

The diagnosis page compares a Groq answer with no historical context against one informed by Hindsight. The “Recalled memories” expander shows the search results used for the latter.

## Setup and run

Python 3.10 or newer is recommended.

1. Create and activate a virtual environment.
2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy `.env.example` to `.env` and set `HINDSIGHT_URL`, `HINDSIGHT_API_KEY` (if required by your Hindsight deployment), and `GROQ_API_KEY`.
4. Start Streamlit:

   ```bash
   streamlit run app.py
   ```

5. To load the sample incidents into the configured Hindsight server, run:

   ```bash
   python seed.py
   ```

`seed.py` retains its sample incidents each time it runs. The app itself uses no local database; Hindsight is the only persistent memory store.
