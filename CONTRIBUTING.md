# Contributing

This is primarily a personal project, but issues and pull requests are welcome.

## Ground rules

1. **Never commit secrets.** No API keys, tokens, chat IDs, or real portfolio data. The `.gitignore` blocks the obvious paths, but check your diffs.
2. **The flag-not-trade principle is non-negotiable.** Contributions must not add automated trading, automated parameter changes, or anything that moves money. The system surfaces information; humans decide.
3. **Honesty over polish.** If a signal is weak or a metric is noisy, the code should say so (sample sizes, caveats, disclosed limitations) rather than present false confidence.
4. **No fabricated precision.** Especially in any AI-narration layer — outputs must be grounded in the structured data, not invented.

## Development

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example ~/.config/guga/.env   # fill in real values, never commit
cp holdings.example.json holdings.json
cp watchlist.example.json watchlist.json
```

## Style

- Python 3.12, standard library where possible.
- Small, verifiable changes. Each module does one thing.
- Comments explain *why*, not *what*.

## Reporting issues

Open an issue describing the behavior, what you expected, and how to reproduce. Do not paste real credentials or account data into issues.
