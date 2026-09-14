from pathlib import Path
from backend.tasks.cleanup_tasks import cleanup_old_files

if __name__ == "__main__":
    print({"removed": cleanup_old_files(Path("data/upload_raw"), older_than_days=7)})
