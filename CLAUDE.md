# langgraph-temporal

Temporal integration for LangGraph — durable execution for stateful AI agents.

## Development
**IMPORTANT** run following commands to verify all changes added into the project. 

```bash
make format   # run code formatters
make lint     # run linter + mypy
make test     # run test suite
make test_integration # run integration test
```

To run a specific test file:

```
TEST=path/to/test.py make test
```

## Key documents

- `REQUIREMENTS.md` — all key requirements for the Temporal integration
- `DESIGN.md` — technical design document

**IMPORTANT:** Make sure all implementations align with both documents. If there is any **valid** deviation (e.g. incorrect initial assumption which requires requirement/design change), update the document to reflect the new behavior.

## Code style

- Do NOT use Sphinx-style double backtick formatting (` ``code`` `). Use single backticks (`` `code` ``) for inline code references in docstrings and comments.
