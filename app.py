# app.py
import os
from flask import Flask, render_template, request, redirect, url_for, flash
from flask_sqlalchemy import SQLAlchemy
from flask_migrate import Migrate
from models import db, Config, BotStatus
from datetime import datetime
from dotenv import load_dotenv
from binance.client import Client as BinanceClient
from binance.enums import *
import logging

# Load environment variables from .env (for local development)
load_dotenv()

app = Flask(__name__)

# Set the secret key from environment variable
app.secret_key = os.getenv('SECRET_KEY')

# Database configuration
app.config['SQLALCHEMY_DATABASE_URI'] = os.getenv('DATABASE_URL')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
migrate = Migrate(app, db)

# Initialize Binance Client for fetching symbols
BINANCE_API_KEY = os.getenv('BINANCE_API_KEY')
BINANCE_API_SECRET = os.getenv('BINANCE_API_SECRET')
binance_client = BinanceClient(BINANCE_API_KEY, BINANCE_API_SECRET)

# Configure logging for symbol fetching errors
logging.basicConfig(
    filename='app.log',
    level=logging.ERROR,
    format='%(asctime)s %(levelname)s %(message)s',
)

# Initialize the database and create tables if they don't exist
with app.app_context():
    db.create_all()
    # Initialize Config and BotStatus if not present
    if Config.query.first() is None:
        config = Config()
        db.session.add(config)
        db.session.commit()
    if BotStatus.query.first() is None:
        status = BotStatus(running=False)
        db.session.add(status)
        db.session.commit()

@app.route('/', methods=['GET', 'POST'])
def index():
    config = Config.query.first()
    status = BotStatus.query.first()
    symbols = []

    # Fetch all USD-M perpetual future symbols from Binance
    try:
        exchange_info = binance_client.futures_exchange_info()
        symbols = [
            s['symbol'] for s in exchange_info['symbols']
            if s['contractType'] == 'PERPETUAL' and s['quoteAsset'] == 'USDT'
        ]
    except Exception as e:
        logging.error(f"Error fetching symbols from Binance: {e}")
        flash('Error fetching symbols from Binance. Please try again later.', 'danger')

    if request.method == 'POST':
        # Update Config
        selected_symbol = request.form['symbol'].upper()
        config.symbol = selected_symbol
        config.timeframe = request.form['timeframe']

        try:
            session_start = datetime.strptime(request.form['session_start'], '%H:%M').time()
            session_end = datetime.strptime(request.form['session_end'], '%H:%M').time()
            config.session_start = session_start
            config.session_end = session_end
        except ValueError:
            flash('Invalid time format. Use HH:MM (24-hour).', 'danger')
            return redirect(url_for('index'))

        try:
            config.sl_amount = float(request.form['sl_amount'])
            config.tsl_step = float(request.form['tsl_step'])
            config.trade_quantity = float(request.form['trade_quantity'])
        except ValueError:
            flash('SL, TSL steps, and Trade Quantity must be numeric.', 'danger')
            return redirect(url_for('index'))

        db.session.commit()
        flash('Configuration updated successfully.', 'success')
        return redirect(url_for('index'))

    return render_template('index.html', config=config, status=status, symbols=symbols)

@app.route('/start', methods=['POST'])
def start_bot():
    status = BotStatus.query.first()
    if not status.running:
        status.running = True
        db.session.commit()
        flash('Bot started.', 'success')
    else:
        flash('Bot is already running.', 'warning')
    return redirect(url_for('index'))

@app.route('/stop', methods=['POST'])
def stop_bot():
    status = BotStatus.query.first()
    if status.running:
        status.running = False
        db.session.commit()
        flash('Bot stopped.', 'success')
    else:
        flash('Bot is not running.', 'warning')
    return redirect(url_for('index'))

if __name__ == '__main__':
    app.run(debug=True)
