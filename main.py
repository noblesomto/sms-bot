import logging
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from config import settings
from db.database import init_db
from scheduler import setup_scheduler
from api.routes import router

# asctime must actually be UTC to match the "UTC" label — the default converter
# uses server-local time (CEST on the VPS), which skewed log prefixes by 2h.
logging.Formatter.converter = time.gmtime
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s UTC %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("SMC Bot starting up")
    init_db()
    logger.info("Database initialised")

    sched = setup_scheduler()
    sched.start()
    logger.info(
        f"Scheduler started — scanning every {settings.SCAN_INTERVAL_MINUTES} min | "
        f"pairs={settings.PAIRS} | timeframes={settings.TIMEFRAMES}"
    )

    yield

    sched.shutdown()
    logger.info("SMC Bot shut down cleanly")


app = FastAPI(
    title="SMC/ICT Chart Scanner Bot",
    description="24/7 Smart Money Concepts trading signal scanner for forex pairs",
    version="1.0.0",
    lifespan=lifespan,
)

app.include_router(router)


if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=False)
