from dotenv import load_dotenv

# Load .env before anything reads configuration. Several modules (embeddings, auth, policy paths)
# read the environment lazily and used to depend on some *other* module having loaded it first.
# Real environment variables always win over the file.
load_dotenv()

from app.utils.logger import configure_logging  # noqa: E402

configure_logging()
