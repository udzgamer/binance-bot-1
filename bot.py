# bot.py
import os
import time
import logging
from datetime import datetime, timedelta
from decimal import Decimal

import pandas as pd
from binance.client import Client
from binance.enums import *
from binance.exceptions import BinanceAPIException, BinanceOrderException
from ta.volatility import AverageTrueRange
from ta.volume import VolumeWeightedAveragePrice
from dotenv import load_dotenv
from models import db, Config, BotStatus
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(
    filename='bot.log',
    level=logging.INFO,
    format='%(asctime)s %(levelname)s %(message)s',
)

# Binance API credentials
API_KEY = os.getenv('BINANCE_API_KEY')
API_SECRET = os.getenv('BINANCE_API_SECRET')

# Initialize Binance Client
client = Client(API_KEY, API_SECRET)

# Database configuration
DATABASE_URL = os.getenv('DATABASE_URL')
engine = create_engine(DATABASE_URL)
Session = sessionmaker(bind=engine)
session = Session()

# Global Variables
sl_order = None
tsl_triggered = False

def get_current_time():
    return datetime.utcnow()

def is_within_session(current_time, config):
    session_start = current_time.replace(hour=config.session_start.hour, minute=config.session_start.minute,
                                        second=config.session_start.second, microsecond=0)
    session_end = session_start + timedelta(hours=21)  # 21 hours session
    # Handle session crossing midnight
    if session_end.day > session_start.day:
        if current_time >= session_start or current_time < session_end:
            return True
    else:
        if session_start <= current_time < session_end:
            return True
    return False

def fetch_klines(symbol, timeframe):
    try:
        klines = client.futures_klines(symbol=symbol, interval=timeframe, limit=100)
        df = pd.DataFrame(klines, columns=[
            'open_time', 'open', 'high', 'low', 'close', 'volume',
            'close_time', 'quote_asset_volume', 'number_of_trades',
            'taker_buy_base_asset_volume', 'taker_buy_quote_asset_volume', 'ignore'
        ])
        df['open'] = df['open'].astype(float)
        df['high'] = df['high'].astype(float)
        df['low'] = df['low'].astype(float)
        df['close'] = df['close'].astype(float)
        df['volume'] = df['volume'].astype(float)
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
        return df
    except Exception as e:
        logging.error(f"Error fetching klines: {e}")
        raise

def calculate_indicators(df):
    try:
        # Calculate VWAP
        vwap = VolumeWeightedAveragePrice(high=df['high'], low=df['low'], close=df['close'], volume=df['volume'], window=14)
        df['vwap'] = vwap.volume_weighted_average_price()

        # Calculate ATR for Supertrend
        atr = AverageTrueRange(high=df['high'], low=df['low'], close=df['close'], window=7)
        df['atr'] = atr.average_true_range()

        # Calculate Basic Upper and Lower Bands
        df['basic_ub'] = (df['high'] + df['low']) / 2 + 2 * df['atr']
        df['basic_lb'] = (df['high'] + df['low']) / 2 - 2 * df['atr']

        # Initialize Final Upper Band and Final Lower Band
        df['final_ub'] = 0.0
        df['final_lb'] = 0.0

        # Initialize Supertrend direction
        df['supertrend'] = True  # True for uptrend, False for downtrend

        for i in range(1, len(df)):
            # Final Upper Band
            if (df['basic_ub'].iloc[i] < df['final_ub'].iloc[i-1]) or (df['close'].iloc[i-1] > df['final_ub'].iloc[i-1]):
                df.at[i, 'final_ub'] = df['basic_ub'].iloc[i]
            else:
                df.at[i, 'final_ub'] = df['final_ub'].iloc[i-1]

            # Final Lower Band
            if (df['basic_lb'].iloc[i] > df['final_lb'].iloc[i-1]) or (df['close'].iloc[i-1] < df['final_lb'].iloc[i-1]):
                df.at[i, 'final_lb'] = df['basic_lb'].iloc[i]
            else:
                df.at[i, 'final_lb'] = df['final_lb'].iloc[i-1]

            # Determine Supertrend
            if df['close'].iloc[i] > df['final_ub'].iloc[i]:
                df.at[i, 'supertrend'] = True
            elif df['close'].iloc[i] < df['final_lb'].iloc[i]:
                df.at[i, 'supertrend'] = False
            else:
                df.at[i, 'supertrend'] = df['supertrend'].iloc[i-1]

        return df
    except Exception as e:
        logging.error(f"Error calculating indicators: {e}")
        raise

def place_stop_limit_order(side, trigger_price, limit_price, quantity):
    try:
        order = client.futures_create_order(
            symbol=SYMBOL,
            side=side,
            type=ORDER_TYPE_STOP_LOSS_LIMIT,
            quantity=quantity,
            price=limit_price,
            stopPrice=trigger_price,
            timeInForce=TIME_IN_FORCE_GTC
        )
        logging.info(f"Placed {side} stop-limit order at trigger price {trigger_price} with limit price {limit_price}")
        return order
    except BinanceAPIException as e:
        logging.error(f"Binance API Exception: {e}")
        raise
    except BinanceOrderException as e:
        logging.error(f"Binance Order Exception: {e}")
        raise
    except Exception as e:
        logging.error(f"Error placing stop-limit order: {e}")
        raise

def cancel_order(order_id):
    try:
        result = client.futures_cancel_order(symbol=SYMBOL, orderId=order_id)
        logging.info(f"Cancelled order {order_id}")
        return result
    except BinanceAPIException as e:
        logging.error(f"Binance API Exception while cancelling order: {e}")
    except BinanceOrderException as e:
        logging.error(f"Binance Order Exception while cancelling order: {e}")
    except Exception as e:
        logging.error(f"Error cancelling order: {e}")

def get_open_orders():
    try:
        orders = client.futures_get_open_orders(symbol=SYMBOL)
        return orders
    except Exception as e:
        logging.error(f"Error fetching open orders: {e}")
        return []

def get_position():
    try:
        positions = client.futures_position_information(symbol=SYMBOL)
        for pos in positions:
            if float(pos['positionAmt']) != 0:
                return pos
        return None
    except Exception as e:
        logging.error(f"Error fetching position: {e}")
        return None

def set_stop_loss(entry_price, side, quantity):
    try:
        if side == SIDE_BUY:
            sl_stop_price = entry_price - SL_AMOUNT
            sl_limit_price = sl_stop_price - 0.5  # Buffer below stop price
            sl_side = SIDE_SELL
        else:
            sl_stop_price = entry_price + SL_AMOUNT
            sl_limit_price = sl_stop_price + 0.5  # Buffer above stop price
            sl_side = SIDE_BUY
        sl_order = place_stop_limit_order(sl_side, sl_stop_price, sl_limit_price, quantity)
        logging.info(f"Set Stop Loss at {sl_stop_price} with limit price {sl_limit_price}")
        return sl_order
    except Exception as e:
        logging.error(f"Error setting stop loss: {e}")

def close_all_positions():
    try:
        position = get_position()
        if position:
            side = SIDE_SELL if float(position['positionAmt']) > 0 else SIDE_BUY
            order = client.futures_create_order(
                symbol=SYMBOL,
                side=side,
                type=ORDER_TYPE_MARKET,
                quantity=abs(float(position['positionAmt']))
            )
            logging.info(f"Closed position by placing MARKET {side} order.")
    except Exception as e:
        logging.error(f"Error closing positions: {e}")

def cancel_all_orders():
    try:
        open_orders = get_open_orders()
        for order in open_orders:
            cancel_order(order['orderId'])
    except Exception as e:
        logging.error(f"Error cancelling all orders: {e}")

def main():
    global sl_order, tsl_triggered

    while True:
        try:
            # Fetch current config and bot status
            config = session.query(Config).first()
            status = session.query(BotStatus).first()

            if not status.running:
                time.sleep(5)
                continue

            SYMBOL = config.symbol
            TIMEFRAME = config.timeframe
            SL_AMOUNT = config.sl_amount
            TSL_STEP = config.tsl_step
            TRADE_QUANTITY = config.trade_quantity

            current_time = get_current_time()
            if is_within_session(current_time, config):
                df = fetch_klines(SYMBOL, TIMEFRAME)
                df = calculate_indicators(df)

                # Get the last two completed candles
                last_two = df.iloc[-3:-1]  # Exclude the latest candle as it may not be closed yet

                # Determine Buy Condition
                buy_condition = (
                    (last_two['close'] > last_two['vwap']).all() and
                    (last_two['supertrend']).all()
                )

                # Determine Sell Condition
                sell_condition = (
                    (last_two['close'] < last_two['vwap']).all() and
                    (~last_two['supertrend']).all()
                )

                # Fetch open orders and current position
                open_orders = get_open_orders()
                current_position = get_position()

                if not current_position:
                    # Not in a position, check for buy/sell conditions
                    if buy_condition:
                        trigger_price = last_two['high'].iloc[-1]
                        limit_price = trigger_price + 0.5  # Adding small buffer above stop price
                        existing_buy_order = next(
                            (order for order in open_orders if order['side'] == SIDE_BUY and order['type'] == ORDER_TYPE_STOP_LOSS_LIMIT),
                            None
                        )
                        if existing_buy_order:
                            if float(existing_buy_order['stopPrice']) != trigger_price:
                                cancel_order(existing_buy_order['orderId'])
                                place_stop_limit_order(SIDE_BUY, trigger_price, limit_price, TRADE_QUANTITY)
                        else:
                            place_stop_limit_order(SIDE_BUY, trigger_price, limit_price, TRADE_QUANTITY)

                    elif sell_condition:
                        trigger_price = last_two['low'].iloc[-1]
                        limit_price = trigger_price - 0.5  # Adding small buffer below stop price
                        existing_sell_order = next(
                            (order for order in open_orders if order['side'] == SIDE_SELL and order['type'] == ORDER_TYPE_STOP_LOSS_LIMIT),
                            None
                        )
                        if existing_sell_order:
                            if float(existing_sell_order['stopPrice']) != trigger_price:
                                cancel_order(existing_sell_order['orderId'])
                                place_stop_limit_order(SIDE_SELL, trigger_price, limit_price, TRADE_QUANTITY)
                        else:
                            place_stop_limit_order(SIDE_SELL, trigger_price, limit_price, TRADE_QUANTITY)
                    else:
                        # Conditions not met, cancel bot's pending stop-limit orders
                        for order in open_orders:
                            if order['type'] == ORDER_TYPE_STOP_LOSS_LIMIT:
                                cancel_order(order['orderId'])
                else:
                    # In a position, set or manage SL and TSL
                    entry_price = float(current_position['entryPrice'])
                    side = SIDE_BUY if float(current_position['positionAmt']) > 0 else SIDE_SELL
                    mark_price = float(client.futures_mark_price(symbol=SYMBOL)['markPrice'])

                    if not sl_order:
                        # Set initial Stop Loss as Stop Limit Order
                        sl_order = set_stop_loss(entry_price, side, TRADE_QUANTITY)

                    else:
                        # Manage SL and TSL
                        sl_order_details = client.futures_get_order(symbol=SYMBOL, orderId=sl_order['orderId'])
                        current_sl_stop_price = float(sl_order_details['stopPrice'])
                        current_sl_limit_price = float(sl_order_details['price'])

                        if side == SIDE_BUY:
                            profit = mark_price - entry_price
                        else:
                            profit = entry_price - mark_price

                        if profit >= SL_AMOUNT + TSL_STEP and not tsl_triggered:
                            # Move SL to break-even
                            if side == SIDE_BUY:
                                new_sl_stop_price = entry_price
                                new_sl_limit_price = new_sl_stop_price - 0.5
                                sl_side = SIDE_SELL
                            else:
                                new_sl_stop_price = entry_price
                                new_sl_limit_price = new_sl_stop_price + 0.5
                                sl_side = SIDE_BUY

                            cancel_order(sl_order['orderId'])
                            sl_order = place_stop_limit_order(sl_side, new_sl_stop_price, new_sl_limit_price, TRADE_QUANTITY)
                            tsl_triggered = True
                            logging.info("Moved Stop Loss to break-even.")

                        elif profit >= SL_AMOUNT + TSL_STEP and tsl_triggered:
                            # Move SL further by TSL_STEP
                            if side == SIDE_BUY:
                                new_sl_stop_price = current_sl_stop_price + TSL_STEP
                                new_sl_limit_price = new_sl_stop_price - 0.5
                                sl_side = SIDE_SELL
                            else:
                                new_sl_stop_price = current_sl_stop_price - TSL_STEP
                                new_sl_limit_price = new_sl_stop_price + 0.5
                                sl_side = SIDE_BUY

                            cancel_order(sl_order['orderId'])
                            sl_order = place_stop_limit_order(sl_side, new_sl_stop_price, new_sl_limit_price, TRADE_QUANTITY)
                            logging.info(f"Moved Trailing Stop Loss by {TSL_STEP} to stop price {new_sl_stop_price} with limit price {new_sl_limit_price}")

        except Exception as e:
            logging.error(f"Unexpected error: {e}")
            time.sleep(5)  # Wait before retrying
            continue

        # Sleep for 1 second before next iteration
        time.sleep(1)

if __name__ == "__main__":
    main()
