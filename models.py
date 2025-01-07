# models.py
from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Config(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    symbol = db.Column(db.String(20), nullable=False, default='ETHUSDT')
    timeframe = db.Column(db.String(10), nullable=False, default='1m')
    session_start = db.Column(db.Time, nullable=False, default='08:00:00')
    session_end = db.Column(db.Time, nullable=False, default='05:00:00')
    sl_amount = db.Column(db.Float, nullable=False, default=25.0)
    tsl_step = db.Column(db.Float, nullable=False, default=10.0)
    trade_quantity = db.Column(db.Float, nullable=False, default=1.0)  # New Column

class BotStatus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    running = db.Column(db.Boolean, nullable=False, default=False)
