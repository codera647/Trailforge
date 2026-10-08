# Third-party dependencies

Original Trailforge code, documentation and synthetic examples are MIT licensed; see [LICENSE](LICENSE). The base runtime declares no external dependencies. Optional and development dependencies are installed separately and retain their own licenses and notices; installing an extra does not relicense its dependencies.

| Component | Use | Upstream license/notice source |
| --- | --- | --- |
| Python | Runtime | [Python licensing](https://docs.python.org/3/license.html) |
| setuptools, build, packaging, pyproject-hooks | Distribution construction | Their installed distribution metadata and upstream repositories |
| psycopg / psycopg-binary | Optional PostgreSQL driver | [Psycopg license and binary notices](https://www.psycopg.org/psycopg3/docs/basic/install.html) and installed metadata |
| jsonschema, referencing, jsonschema-specifications, attrs, rpds-py | Optional schemas/PostgreSQL validation | Their installed distribution metadata and upstream repositories |
| LangGraph and its transitive dependencies | Optional scheduling facade | [LangGraph source](https://github.com/langchain-ai/langgraph) and installed distribution metadata |
| PostgreSQL container | Disposable conformance service | [PostgreSQL license](https://www.postgresql.org/about/licence/) and official image notices |
| Python container | Linux boundary conformance | [Official Python image](https://github.com/docker-library/python) and image notices |
| GitHub Actions | CI orchestration | Each pinned action repository's license and notices |

Dependency lock files record versions used by verification. Receipts record actual installed versions. Containers and optional packages are not vendored into the base wheel. If redistributing those dependencies or images, preserve their upstream notices and comply with their respective licenses.

No supplied third-party book, private PDF, customer repository or private diagnostic is part of this distribution.
