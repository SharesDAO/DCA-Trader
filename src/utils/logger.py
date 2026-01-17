"""
Logging utilities for DCA Trading Bot.
"""
import logging
import sys
from pathlib import Path
from logging.handlers import TimedRotatingFileHandler


def setup_logging(log_level: str = 'INFO'):
    """
    Setup logging configuration with daily rotation.
    
    Logs are rotated daily at midnight and kept for 7 days.
    
    Args:
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
    """
    # Create logs directory
    project_root = Path(__file__).parent.parent.parent
    log_dir = project_root / 'logs'
    log_dir.mkdir(exist_ok=True)
    
    # Configure logging with rotation
    log_file = log_dir / 'bot.log'
    
    # Create rotating file handler
    # - when='midnight': Rotate at midnight
    # - interval=1: Rotate every 1 day
    # - backupCount=7: Keep 7 days of logs
    file_handler = TimedRotatingFileHandler(
        filename=log_file,
        when='midnight',
        interval=1,
        backupCount=7,
        encoding='utf-8'
    )
    file_handler.suffix = '%Y-%m-%d'  # Log files: bot.log.2026-01-11
    
    # Console handler
    console_handler = logging.StreamHandler(sys.stdout)
    
    # Set format
    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
    file_handler.setFormatter(formatter)
    console_handler.setFormatter(formatter)
    
    # Configure root logger
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        handlers=[file_handler, console_handler]
    )
    
    logger = logging.getLogger(__name__)
    logger.info(f"Logging initialized - Level: {log_level}")
    logger.info(f"Log file: {log_file} (rotates daily, keeps 7 days)")
