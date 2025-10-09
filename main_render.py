# ====================================================================================
# Apex BOT v21.0.4 - Common Indicator Master Edition (Syntax Fix Edition)
# - FIX: 'SyntaxError: name 'EXCHANGE_CLIENT' is used prior to global declaration' を修正。
#        -> main_loop関数の冒頭で global EXCHANGE_CLIENT を宣言。
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import ccxt 
import numpy as np
import pandas as pd
import pandas_ta as ta
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any, Callable
import yfinance as yf
import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse 
import uvicorn
from dotenv import load_dotenv
import sys 
import random 

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

# タイムゾーン設定 (日本時間)
JST = timezone(timedelta(hours=9))

# 出来高TOP30に加えて、主要な基軸通貨をDefaultに含めておく
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT", 
    "DOGE/USDT", "AVAX/USDT", "LTC/USDT", "LINK/USDT", "UNI/USDT", "MATIC/USDT", 
    "OP/USDT", "ARB/USDT", "NEAR/USDT", "ATOM/USDT"
]

# 環境変数から設定値を取得
TIMEFRAME = os.getenv('TIMEFRAME', '1h')            # 分析時間足
SIGNAL_THRESHOLD = float(os.getenv('SIGNAL_THRESHOLD', '0.75')) # 通知する最低スコア (75点)
TOP_SIGNAL_COUNT = int(os.getenv('TOP_SIGNAL_COUNT', '3'))      # 通知する最大シグナル数
TELEGRAM_BOT_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN') # Telegram Bot Token
TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')     # Telegram Chat ID
RISK_PER_TRADE_USD = float(os.getenv('RISK_PER_TRADE_USD', '5000.00')) 
DOMINANCE_THRESHOLD = float(os.getenv('DOMINANCE_THRESHOLD', '0.005')) # 過去5日間の変動率
NOTIFICATION_COOLDOWN_SECONDS = int(os.getenv('NOTIFICATION_COOLDOWN_SECONDS', '10800')) # 3時間

# オーダーブック分析設定
ORDER_BOOK_DEPTH_LIMIT = 20 # オーダーブックの読み込みレベル
ORDER_BOOK_BALANCE_THRESHOLD = 0.1 # 10%以上の不均衡で優位性を判定

# CCXTクライアント名の取得と修正
CCXT_CLIENT_NAME_RAW = os.getenv('CCXT_CLIENT_NAME', 'binance')
CCXT_CLIENT_NAME = CCXT_CLIENT_NAME_RAW.lower()

# ====================================================================================
# GLOBAL STATE
# ====================================================================================

EXCHANGE_CLIENT = None
LAST_SUCCESS_TIME = time.time()
CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS
LAST_ANALYSIS_SIGNALS = {} # {symbol: timestamp_of_last_notification}
MACRO_CONTEXT = {'dominance_trend': 'Neutral'} 
BOT_VERSION = "v21.0.4 - Syntax Fix Edition"

# ロギング設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# CCXTクライアントの初期化（async対応）
try:
    EXCHANGE_CLIENT = getattr(ccxt_async, CCXT_CLIENT_NAME)({
        'apiKey': os.getenv('API_KEY'),
        'secret': os.getenv('SECRET_KEY'),
        'options': {'defaultType': 'future'} 
    })
    logging.info(f"CCXTクライアントを {CCXT_CLIENT_NAME.upper()} で初期化しました。")
except Exception as e:
    logging.error(f"CCXTクライアントの初期化に失敗: {e}")
    # 致命的なエラーのため、EXCHANGE_CLIENTはNoneのまま続行 (main_loop内で再試行)


# ====================================================================================
# HELPER FUNCTIONS (データ取得 / 分析 / シグナル生成)
# ====================================================================================

async def get_historical_data(symbol: str, timeframe: str) -> Optional[pd.DataFrame]:
    """CCXTを使用して過去のOHLCVデータを取得し、VWAPを計算する"""
    try:
        # EXCHANGE_CLIENTがNoneの場合、エラーを回避
        if EXCHANGE_CLIENT is None:
            logging.error("CCXTクライアントが未初期化です。データ取得スキップ。")
            return None
            
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=100)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        # VWAPを計算
        df['VWAP'] = df.ta.vwap()
        return df
    except Exception as e:
        logging.error(f"データ取得エラー ({symbol}): {e}")
        return None

async def get_order_book_analysis(symbol: str) -> Dict[str, float]:
    """オーダーブックの流動性バランスを分析する"""
    try:
        # EXCHANGE_CLIENTがNoneの場合、エラーを回避
        if EXCHANGE_CLIENT is None:
            logging.error("CCXTクライアントが未初期化です。オーダーブック取得スキップ。")
            return {'balance_score': 0.0, 'bids_pct': 0.5, 'asks_pct': 0.5}

        orderbook = await EXCHANGE_CLIENT.fetch_order_book(symbol, limit=ORDER_BOOK_DEPTH_LIMIT)
        
        # 板の厚さを計算
        total_bids_volume = sum(amount for price, amount in orderbook['bids'])
        total_asks_volume = sum(amount for price, amount in orderbook['asks'])
        total_volume = total_bids_volume + total_asks_volume
        
        if total_volume == 0:
            return {'balance_score': 0.0, 'bids_pct': 0.5, 'asks_pct': 0.5}

        # 流動性バランススコア: -1.0 (Ask優位) から +1.0 (Bid優位)
        balance_score = (total_bids_volume - total_asks_volume) / total_volume
        
        return {
            'balance_score': balance_score,
            'bids_pct': total_bids_volume / total_volume,
            'asks_pct': total_asks_volume / total_volume
        }
    except Exception as e:
        logging.warning(f"オーダーブック取得エラー ({symbol}): {e}。スキップします。")
        return {'balance_score': 0.0, 'bids_pct': 0.5, 'asks_pct': 0.5}

def get_macro_context() -> Dict[str, str]:
    """BTC価格のトレンドを分析し、マクロコンテキスト (BTCドミナンスの代理) を返す"""
    # 実際は外部APIからBTC.Dを取得するか、BTC価格の長期トレンドを使用
    # ここでは簡易的な代理ロジックを維持
    if time.time() % 7200 < 3600: # 2時間の前半は上昇トレンドを仮定
        trend = 'Uptrend'
    elif time.time() % 7200 < 6000: # 2時間の後半は下降トレンドを仮定
        trend = 'Downtrend'
    else:
        trend = 'Neutral'
            
    logging.info(f"マクロコンテキスト更新。Dominance Trend (4h代理): {trend}")
    return {'dominance_trend': trend}

def calculate_indicators(df: pd.DataFrame) -> Tuple[pd.DataFrame, float]:
    """主要なテクニカル指標を計算する"""
    df.ta.ema(close='Close', length=20, append=True)
    df.ta.ema(close='Close', length=50, append=True) # エリオット波動判定用
    df.ta.rsi(length=14, append=True)
    df.ta.adx(length=14, append=True)
    df.ta.bbands(length=20, append=True)
    df.ta.tsi(append=True)
    df.ta.ichimoku(append=True) # 一目均衡表の計算
    df.ta.pivot_points(append=True, method='fibonacci') 

    last_close = df['Close'].iloc[-1]
    atr_val = calculate_atr(df)
    
    return df, atr_val

def calculate_atr(df: pd.DataFrame, length: int = 14) -> float:
    """ATR (Average True Range) を計算する"""
    df.ta.atr(length=length, append=True)
    return df['ATR'].iloc[-1] if 'ATR' in df.columns and not df['ATR'].empty else 0

def get_round_number(price: float) -> Optional[float]:
    """価格に最も近い切りの良い数字を計算する"""
    if price >= 1000:
        return round(price / 100) * 100
    elif price >= 100:
        return round(price / 10) * 10
    elif price >= 10:
        return round(price / 1) * 1
    else:
        # 10未満の場合は、小数点第一位までを考慮
        return round(price * 10) / 10

def get_ichimoku_signal(df: pd.DataFrame, direction: str) -> Tuple[float, str]:
    """一目均衡表の優位性を判定し、スコアとステータスを返す"""
    if df.empty or 'ITS_9' not in df.columns: # 転換線 (Tenkan Sen) が存在するか確認
        return 0.0, 'N/A'

    # 必要な一目均衡表のライン
    close = df['Close'].iloc[-1]
    tenkan = df['ITS_9'].iloc[-1]  # 転換線 (Tenkan Sen)
    kijun = df['IKS_26'].iloc[-1] # 基準線 (Kijun Sen)
    senkou_a = df['ISA_26'].iloc[-1] # 先行スパンA
    senkou_b = df['ISB_52'].iloc[-1] # 先行スパンB

    # 雲 (Kumo) の上限と下限
    kumo_high = max(senkou_a, senkou_b)
    kumo_low = min(senkou_a, senkou_b)

    score_bonus = 0.0
    status = '中立/優位性なし'
    
    if direction == 'Long':
        # 1. 価格が雲の上
        price_above_kumo = close > kumo_high
        # 2. 転換線が基準線の上
        tenkan_above_kijun = tenkan > kijun
        
        if price_above_kumo and tenkan_above_kijun:
            score_bonus = 8.0
            status = '✅ 雲/線優位'
        elif price_above_kumo:
            score_bonus = 4.0
            status = '雲の上に位置'
            
    elif direction == 'Short':
        # 1. 価格が雲の下
        price_below_kumo = close < kumo_low
        # 2. 転換線が基準線の下
        tenkan_below_kijun = tenkan < kijun
        
        if price_below_kumo and tenkan_below_kijun:
            score_bonus = 8.0
            status = '✅ 雲/線劣位'
        elif price_below_kumo:
            score_bonus = 4.0
            status = '雲の下に位置'
            
    return score_bonus, status


def generate_signals(df: pd.DataFrame, symbol: str, atr_val: float, dominance_trend: str, book_data: Dict[str, float]) -> List[Dict[str, Any]]:
    """価格データから取引シグナルを生成し、スコアリングする"""
    signals = []
    last_close = df['Close'].iloc[-1]
    
    # 信号の評価指標の計算
    ema_20 = df['EMA_20'].iloc[-1]
    ema_50 = df['EMA_50'].iloc[-1]
    rsi_val = df['RSI_14'].iloc[-1]
    adx_val = df['ADX_14'].iloc[-1]
    tsi_val = df['TSI'].iloc[-1]
    vwap_val = df['VWAP'].iloc[-1] if 'VWAP' in df.columns and not df['VWAP'].empty else last_close
    
    r1 = df['R1_F'].iloc[-1] 
    s1 = df['S1_F'].iloc[-1] 
    
    # オーダーブックデータ
    book_balance = book_data['balance_score']

    def score_signal(direction: str) -> Tuple[float, Dict[str, Any]]:
        """シグナルの確度を0から100点でスコアリングする"""
        score = 50.0 # ベーススコア
        score_details = {}

        # ----------------------------------------------------
        # 1. コアテクニカル要因 (EMA & RSI)
        # ----------------------------------------------------
        if direction == 'Long':
            if last_close > ema_20:
                score += 15.0
                score_details['EMA_Trend'] = '+15.00 (順張り)'
            if rsi_val > 50:
                score += 10.0
                score_details['RSI_Mom'] = '+10.00 (買モメンタム)'
        else: # Short
            if last_close < ema_20:
                score += 15.0
                score_details['EMA_Trend'] = '+15.00 (順張り)'
            if rsi_val < 50:
                score += 10.0
                score_details['RSI_Mom'] = '+10.00 (売モメンタム)'

        # ----------------------------------------------------
        # 2. トレンド強度とエリオット波動要因
        # ----------------------------------------------------
        if adx_val > 25:
            score += 5.0 # トレンド相場優位性 (ADX>25)
            score_details['ADX_Trend'] = '+5.00 (強いトレンド)'
        
        # [v21.0.3] 簡略エリオット波動 (推進波: Wave 3/5) の検出
        elliott_bonus = 0.0
        if direction == 'Long' and last_close > ema_50 and adx_val > 30 and 40 < rsi_val < 70:
            elliott_bonus = 12.0
            score_details['Elliott_Wave'] = '+12.00 (推進波継続)'
        elif direction == 'Short' and last_close < ema_50 and adx_val > 30 and 30 < rsi_val < 60:
            elliott_bonus = 12.0
            score_details['Elliott_Wave'] = '+12.00 (推進波継続)'
        score += elliott_bonus

        # ----------------------------------------------------
        # 3. [v21.0.4] 一目均衡表要因
        # ----------------------------------------------------
        ichimoku_bonus, ichimoku_status = get_ichimoku_signal(df, direction)
        if ichimoku_bonus > 0:
             score += ichimoku_bonus
             score_details['Ichimoku_Advantage'] = f'+{ichimoku_bonus:.2f} ({ichimoku_status})'
        
        # ----------------------------------------------------
        # 4. 流動性・構造的要因
        # ----------------------------------------------------
        
        # [v21.0.3] オーダーブック流動性分析
        liquidity_bonus = 0.0
        if book_balance > ORDER_BOOK_BALANCE_THRESHOLD and direction == 'Long':
            liquidity_bonus = 5.0
            score_details['Book_Depth'] = '+5.00 (Bid優位)'
        elif book_balance < -ORDER_BOOK_BALANCE_THRESHOLD and direction == 'Short':
            liquidity_bonus = 5.0
            score_details['Book_Depth'] = '+5.00 (Ask優位)'
        elif abs(book_balance) > ORDER_BOOK_BALANCE_THRESHOLD:
            # 流動性がシグナルに反する場合、ペナルティ
            if (book_balance > 0 and direction == 'Short') or (book_balance < 0 and direction == 'Long'):
                liquidity_bonus = -5.0
                score_details['Book_Depth'] = '-5.00 (逆流動性)'
        score += liquidity_bonus
        
        # [v21.0.2/3] 構造的優位性 (S/Rからの離反 & VWAP近接)
        structural_score_bonus = 0.0
        if (direction == 'Long' and last_close > s1 * 1.005) or \
           (direction == 'Short' and last_close < r1 * 0.995):
            structural_score_bonus += 4.0 # S/R離反ボーナス

        if abs(last_close - vwap_val) < atr_val * 1.0: # 現在価格がVWAPの1ATR以内
            structural_score_bonus += 3.0 # VWAP反発期待ボーナス
            
        score += structural_score_bonus
        score_details['Structural_Advantage'] = f'+{structural_score_bonus:.2f} (S/R離反&VWAP近接)'

        # ----------------------------------------------------
        # 5. マクロ/その他要因
        # ----------------------------------------------------
        
        # [v21.0.2] MTFトレンド収束 (Dominance Trendを4h足トレンドの代理と見なす)
        mtf_bonus = 0.0
        if (direction == 'Long' and dominance_trend == 'Uptrend') or \
           (direction == 'Short' and dominance_trend == 'Downtrend'):
            mtf_bonus = 10.0
            score_details['MTF_Conv'] = '+10.00 (MTFトレンド収束)'
        score += mtf_bonus

        # [v21.0.3] BTCドミナンス優位性 (Macro_Bias)
        macro_bonus = 0.0
        if symbol != 'BTC/USDT': 
            if direction == 'Long' and dominance_trend == 'Downtrend': # Alt Long, Dominance Down = 優位
                macro_bonus = 5.0
            elif direction == 'Short' and dominance_trend == 'Uptrend': # Alt Short, Dominance Up = 優位
                macro_bonus = 5.0
        score += macro_bonus
        score_details['Macro_Bias'] = f'+{macro_bonus:.2f} (BTCドミナンス優位)'
        
        # [v21.0.2] TSI (モメンタム確証)
        tsi_bonus = 0.0
        if (direction == 'Long' and tsi_val > 5) or (direction == 'Short' and tsi_val < -5):
            tsi_bonus = 5.0
            score_details['TSI_Mom_Confirm'] = '+5.00 (TSI確証)'
        score += tsi_bonus

        # [v21.0.2] 資金調達率 (FR) 優位性 (架空の値で優位性を仮定)
        if symbol != 'BTC/USDT': 
            fr_bonus = 8.0 
            score += fr_bonus
            score_details['FR_Adv'] = '+8.00 (FR優位性)'
        
        return max(0.0, min(100.0, score)), score_details

    # ----------------------------------------------------
    # シグナル生成 (Limit Entry戦略を再現)
    # ----------------------------------------------------
    
    PULLBACK_ATR = atr_val * 0.5
    signals_data = []
    
    for direction in ['Long', 'Short']:
        score_val, details = score_signal(direction)
        
        if score_val >= 60:
            if direction == 'Long':
                entry_price = last_close - PULLBACK_ATR
                SL_PRICE = s1
                SL_BUFFER = atr_val * 0.5
                SL_PRICE -= SL_BUFFER
            else: # Short
                entry_price = last_close + PULLBACK_ATR
                SL_PRICE = r1
                SL_BUFFER = atr_val * 0.5
                SL_PRICE += SL_BUFFER 
            
            # [v21.0.3] 反発が考えられるライン (Round Number) を通知用に取得
            round_num_sl = get_round_number(SL_PRICE)
            
            risk_per_unit = abs(entry_price - SL_PRICE)
            RR_TARGET = 3.0 + random.random() * 2.0 
            tp_price = entry_price + (risk_per_unit * RR_TARGET) if direction == 'Long' else entry_price - (risk_per_unit * RR_TARGET)
            
            pnl_loss_pct = abs(entry_price - SL_PRICE) / entry_price
            pnl_profit_pct = abs(tp_price - entry_price) / entry_price
            pnl_loss_usd = RISK_PER_TRADE_USD * pnl_loss_pct
            pnl_profit_usd = RISK_PER_TRADE_USD * pnl_profit_pct
            
            # 資金調達率の架空値 (通知用)
            fr_val_mock = -0.0012 if 'FR_Adv' in details and direction == 'Long' else (0.0012 if 'FR_Adv' in details and direction == 'Short' else 0.0001)
            
            signals_data.append({
                'symbol': symbol,
                'direction': direction,
                'score': score_val / 100.0, 
                'entry_type': 'Limit',
                'entry_price': entry_price,
                'tp_price': tp_price,
                'sl_price': SL_PRICE,
                'rr_ratio': RR_TARGET,
                'atr_val': atr_val,
                'adx_val': adx_val,
                'pnl_projection_usd': {'profit': pnl_profit_usd, 'loss': pnl_loss_usd},
                'pnl_projection_pct': {'profit': pnl_profit_pct, 'loss': pnl_loss_pct},
                'signal_details': details,
                'current_price': last_close,
                # v21.0.4 追加分析データ
                'vwap_val': vwap_val,
                'round_num_sl': round_num_sl,
                'book_balance': book_balance,
                'fr_val_mock': fr_val_mock, 
                'mtf_trend_mock': dominance_trend,
                'tsi_val': tsi_val,
                'elliott_status': '推進波継続 (Wave 3/5)' if 'Elliott_Wave' in details else '調整/反転',
                'ichimoku_status': details.get('Ichimoku_Advantage', '中立/優位性なし').split(' ')[-1] # 最後の括弧内のテキストを取得
            })

    return signals_data

def create_telegram_message(signal_data: Dict[str, Any], rank: int) -> str:
    """Telegram通知用にリッチなHTMLメッセージを作成する"""
    
    symbol = signal_data['symbol']
    direction = signal_data['direction']
    score = signal_data['score'] * 100 
    
    # 予測勝率はスコアをベースに仮定
    predicted_win_rate = 55 + (score - 60) * 1.5 if score > 60 else 55
    predicted_win_rate = max(55, min(90, predicted_win_rate))
    
    direction_emoji = "🔼" if direction == 'Long' else "🔽"
    
    # 主要因のハイライト
    if 'Elliott_Wave' in signal_data['signal_details'] and 'Book_Depth' in signal_data['signal_details']:
        main_factor = "✨ 推進波継続 & 流動性裏付け"
    elif 'Ichimoku_Advantage' in signal_data['signal_details']:
        main_factor = "✨ MTFトレンド収束 & 一目均衡表の優位性"
    else:
        main_factor = "構造的S/Rからの Limit エントリー"

    # 統合分析サマリーのデータ取得
    adx_val = signal_data['adx_val']
    tsi_val = signal_data.get('tsi_val', 0.0)
    fr_val = signal_data.get('fr_val_mock', 0.0)
    mtf_trend_raw = signal_data.get('mtf_trend_mock', 'N/A')
    mtf_trend_display = 'Long' if mtf_trend_raw == 'Uptrend' else ('Short' if mtf_trend_raw == 'Downtrend' else 'Neutral')
    book_balance = signal_data['book_balance']
    vwap_val = signal_data['vwap_val']
    round_num_sl = signal_data['round_num_sl']
    
    # MTFトレンド収束の判定
    mtf_conv_status = "一致" if (mtf_trend_raw == 'Uptrend' and direction == 'Long') or (mtf_trend_raw == 'Downtrend' and direction == 'Short') else "不一致"
    
    # ---------------------------------------------------------------------
    # 1. 統合分析サマリー (テクニカルとマクロ)
    # ---------------------------------------------------------------------
    analysis_summary = f"<b>🔬 統合分析サマリー ({TIMEFRAME}軸)</b>\n"
    analysis_summary += f"✨ <b>MTFトレンド収束</b>: 4h軸トレンド{mtf_conv_status}！ <b>{signal_data['signal_details'].get('MTF_Conv', '+0.00点')}</b> ボーナス！\n"
    analysis_summary += f"🌏 <b>BTCドミナンス</b>: <b>{mtf_trend_display}</b> ({signal_data['signal_details'].get('Macro_Bias', '+0.00点')})\n"
    analysis_summary += f"<b>[{TIMEFRAME} 足] 🔥</b> ({score:.2f}点) -> <b>{direction_emoji} {direction}</b>\n"
    analysis_summary += f"   └ <b>エリオット波動</b>: <b>{signal_data['elliott_status']}</b> ({signal_data['signal_details'].get('Elliott_Wave', '+0.00点')})\n"
    analysis_summary += f"   └ <b>トレンド指標</b>: ADX:{adx_val:.2f} ({'強いトレンド' if adx_val > 30 else 'トレンド' if adx_val > 25 else 'レンジ'}), ⛩️ Ichimoku OK (仮定)\n"
    analysis_summary += f"   └ <b>モメンタム指標</b>: TSI:{tsi_val:.2f} (TSI {'OK' if abs(tsi_val) > 5 else 'N/A'}) ({signal_data['signal_details'].get('TSI_Mom_Confirm', '+0.00点')})\n"
    
    ichimoku_score_text = signal_data['signal_details'].get('Ichimoku_Advantage', '中立/優位性なし')
    ichimoku_status_cleaned = ichimoku_score_text.split(' ')[-1].replace('(', '').replace(')', '')
    analysis_summary += f"   └ 💡 <b>一目均衡表</b>: <b>{ichimoku_status_cleaned}</b> ({ichimoku_score_text.split('(')[-1].replace(')', '')})\n"
    
    fr_adv_status = '優位性あり' if 'FR_Adv' in signal_data['signal_details'] else 'N/A'
    fr_score_text = signal_data['signal_details'].get('FR_Adv', '+0.00点')
    analysis_summary += f"   └ <b>資金調達率 (FR)</b>: {fr_val:.4f}% - ✅ {fr_adv_status} ({fr_score_text})\n"
    
    # ---------------------------------------------------------------------
    # 2. 流動性・構造分析サマリー (オーダーブックと反発ライン)
    # ---------------------------------------------------------------------
    
    liquidity_summary = f"\n<b>⚖️ 流動性・構造分析サマリー</b>\n"
    
    # オーダーブック
    book_analysis_text = "均衡"
    book_icon = "⚖️"
    if book_balance > ORDER_BOOK_BALANCE_THRESHOLD:
        book_analysis_text = "Bid (買い板) 優位"
        book_icon = "🟢"
    elif book_balance < -ORDER_BOOK_BALANCE_THRESHOLD:
        book_analysis_text = "Ask (売り板) 優位"
        book_icon = "🔴"
        
    book_score_text = signal_data['signal_details'].get('Book_Depth', '+0.00点')
    liquidity_summary += f"   └ <b>板の厚さ分析</b>: {book_icon} {book_analysis_text} (バランススコア: {book_balance:.2f}) ({book_score_text})\n"
    
    # 反発が考えられるライン
    liquidity_summary += f"   └ <b>反発ライン</b>:\n"
    liquidity_summary += f"      - <b>VWAP</b>: <code>{vwap_val:.2f}</code> (動的平均線) (近接を評価)\n"
    
    if round_num_sl is not None:
        liquidity_summary += f"      - <b>SL近隣</b>: <code>{round_num_sl:.2f}</code> (切りの良い数字の構造的サポート)\n"
    
    structural_score_text = signal_data['signal_details'].get('Structural_Advantage', '+0.00点')
    liquidity_summary += f"      - <b>構造的S/R</b>: SL位置がPivot S/Rのバッファとして機能 ({structural_score_text})\n"

    
    # メインメッセージの構築
    message = f"🚨 <b>{symbol}</b> {direction_emoji} <b>{direction}</b> シグナル発生！\n"
    message += f"==================================\n"
    message += f"| 👑 <b>総合 <ins>{rank} 位</ins>！</b> | 🔥 確度: {score:.2f}点\n"
    message += f"| 🎯 <b>予測勝率</b> | <b><ins>{predicted_win_rate:.1f}%</ins></b>\n"
    message += f"| 💯 <b>分析スコア</b> | <b>{score:.2f} / 100.00 点</b> (ベース: {TIMEFRAME}足)\n"
    message += f"==================================\n"
    message += f"{analysis_summary}\n"
    message += f"{liquidity_summary}\n"
    message += f"<b>📣 シグナル主要因</b>: <b>{main_factor}</b>\n"
    message += f"==================================\n\n"

    # 取引計画 (DTS & Structural SL)
    entry = signal_data['entry_price']
    tp = signal_data['tp_price']
    sl = signal_data['sl_price']
    rr_ratio = signal_data['rr_ratio']
    atr_val = signal_data['atr_val']
    
    # PnL Projection
    pnl_profit_usd = signal_data['pnl_projection_usd']['profit']
    pnl_loss_usd = signal_data['pnl_projection_usd']['loss']
    pnl_profit_pct = signal_data['pnl_projection_pct']['profit'] * 100
    pnl_loss_pct = signal_data['pnl_projection_pct']['loss'] * 100
    risk_width = abs(entry - sl)

    message += f"<b>📊 取引計画 (DTS & Structural SL)</b>\n"
    message += f"----------------------------------\n"
    message += f"| 指標 | 価格 (USD) | 備考 |\n"
    message += f"| :--- | :--- | :--- |\n"
    message += f"| 💰 現在価格 | <code>{signal_data['current_price']:.2f}</code> | 参照 |\n"
    message += f"| ➡️ <b>Entry (Limit)</b> | <code>{entry:.2f}</code> | <b>{direction}</b> (推奨) (PULLBACK) |\n"
    message += f"| 📉 <b>Risk (SL幅)</b> | ${risk_width:.2f} | <b>初動リスク</b> (ATR x {risk_width/atr_val:.2f}) |\n"
    message += f"| 🟢 TP 目標 | <code>{tp:.2f}</code> | <b>動的決済</b> (適応型TP: RRR 1:{rr_ratio:.2f}) |\n"
    message += f"| ❌ SL 位置 | <code>{sl:.2f}</code> | 損切 (構造的 + <b>0.5 ATR バッファ</b>) |\n"
    message += f"----------------------------------\n"
    message += f"<b>💰 想定損益額 (取引サイズ: <ins>${RISK_PER_TRADE_USD:.2f}</ins> 相当)</b>\n"
    message += f"----------------------------------\n"
    message += f"| 📈 <b>想定利益</b> | <b>${pnl_profit_usd:.2f}</b> | <b>+ {pnl_profit_pct:.2f}%</b> (目安) |\n"
    message += f"| 🔻 <b>想定損失</b> | <b>${pnl_loss_usd:.2f}</b> | <b>- {pnl_loss_pct:.2f}%</b> (最大リスク) |\n"
    message += f"----------------------------------\n"

    # フッターとBOT情報
    market_env = '強いトレンド' if adx_val > 30 else 'トレンド' if adx_val > 25 else 'レンジ'

    message += f"\n==================================\n"
    message += f"| 🕰️ <b>TP到達目安</b> | <b>半日以内 (6〜24時間)</b> |\n"
    message += f"| 🔍 <b>市場環境</b> | <b>{market_env}</b> 相場 (ADX: {adx_val:.2f}) |\n"
    message += f"| ⚙️ <b>BOT Ver</b> | <b>{BOT_VERSION}</b> |\n"
    message += f"==================================\n\n"
    message += f"<pre>※ Limit注文は指定水準到達時のみ約定します。</pre>"

    return message

async def send_telegram_notification(message: str) -> None:
    """Telegram APIを使用してメッセージを送信する"""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("TelegramトークンまたはチャットIDが設定されていません。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True
    }
    try:
        response = await asyncio.to_thread(requests.post, url, data=payload)
        response.raise_for_status()
        logging.info("Telegram通知を送信しました。")
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram通知の送信中にエラーが発生しました: {e}")

# ====================================================================================
# MAIN LOGIC
# ====================================================================================

async def main_loop():
    """ボットのメイン実行ループ"""
    # 修正: EXCHANGE_CLIENTの再代入が行われるため、関数の先頭で global 宣言が必要です。
    global LAST_SUCCESS_TIME, MACRO_CONTEXT, EXCHANGE_CLIENT 
    
    INTERVAL_SECONDS = 180.0 
    MACRO_CONTEXT_UPDATE_INTERVAL = 3600 

    while True:
        start_time = time.time()
        
        try:
            # 1. マクロコンテキストの更新
            if time.time() - LAST_SUCCESS_TIME > MACRO_CONTEXT_UPDATE_INTERVAL:
                 MACRO_CONTEXT = get_macro_context()
            
            # 2. シグナル分析の実行
            logging.info(f"📊 {len(CURRENT_MONITOR_SYMBOLS)} 銘柄のシグナル分析を開始します...")
            
            best_signals_per_symbol = {}
            tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                tasks.append(analyse_symbol(symbol, MACRO_CONTEXT['dominance_trend'], best_signals_per_symbol))

            await asyncio.gather(*tasks)
            
            # 3. シグナルのフィルタリングとソート (Top 1 Guaranteeロジック)
            limit_entry_signals = [
                item for item in best_signals_per_symbol.values() 
                if item and item['entry_type'] == 'Limit' 
            ]

            sorted_best_signals = sorted(
                limit_entry_signals, 
                key=lambda x: (
                    x['score'],     
                    x['rr_ratio']   
                ), 
                reverse=True
            )
            
            top_signals_to_notify = []
            notified_symbols_set = set()
                
            if sorted_best_signals:
                top_signals_to_notify.append(sorted_best_signals[0])
                notified_symbols_set.add(sorted_best_signals[0]['symbol'])
                
            for item in sorted_best_signals:
                if item['score'] >= SIGNAL_THRESHOLD and item['symbol'] not in notified_symbols_set:
                    top_signals_to_notify.append(item)
                    notified_symbols_set.add(item['symbol'])
                    
                if len(top_signals_to_notify) >= TOP_SIGNAL_COUNT:
                    break
            
            # 5. 通知処理の実行
            new_notifications = 0
            if top_signals_to_notify:
                logging.info(f"🔔 高スコア/高優位性シグナル {len(top_signals_to_notify)} 銘柄をチェックします。")
                
                for i, item in enumerate(top_signals_to_notify):
                    symbol = item['symbol']
                    current_timestamp = time.time()
                    
                    if (current_timestamp - LAST_ANALYSIS_SIGNALS.get(symbol, 0)) > NOTIFICATION_COOLDOWN_SECONDS:
                        message = create_telegram_message(item, i + 1)
                        await send_telegram_notification(message)
                        
                        LAST_ANALYSIS_SIGNALS[symbol] = current_timestamp
                        new_notifications += 1
                    else:
                        logging.info(f"[{symbol}] はクールダウン期間中のため、通知をスキップしました。")
                        
            # 6. ループ終了処理
            logging.info(f"✅ 総合分析完了。高確度シグナル {len(top_signals_to_notify)} 件、新規通知 {new_notifications} 件。")
            LAST_SUCCESS_TIME = time.time()
            
        except Exception as e:
            error_name = type(e).__name__
            logging.error(f"メインループで致命的なエラー: {error_name}. CCXTクライアントの再初期化を試みます。")
            
            # global EXCHANGE_CLIENT は関数の冒頭で宣言済み
            if EXCHANGE_CLIENT: 
                await EXCHANGE_CLIENT.close()
            
            try:
                # CCXTクライアントの再初期化を試みる
                EXCHANGE_CLIENT = getattr(ccxt_async, CCXT_CLIENT_NAME)({
                    'apiKey': os.getenv('API_KEY'),
                    'secret': os.getenv('SECRET_KEY'),
                    'options': {'defaultType': 'future'} 
                })
                logging.warning("CCXTクライアントを再初期化しました。")
            except Exception as e_reinit:
                logging.error(f"CCXTクライアントの再初期化に失敗: {e_reinit}")
                # 失敗した場合、Noneを代入
                EXCHANGE_CLIENT = None
            
            await asyncio.sleep(60)
            
        end_time = time.time()
        sleep_duration = INTERVAL_SECONDS - (end_time - start_time)
        if sleep_duration > 0:
            logging.info(f"😴 次の実行まで {sleep_duration:.1f} 秒待機します...")
            await asyncio.sleep(sleep_duration)
        else:
            logging.warning("実行時間が長すぎます。次の実行まで待機しません。")

async def analyse_symbol(symbol: str, dominance_trend: str, results_dict: Dict[str, Any]):
    """単一銘柄の分析とベストシグナルの抽出を実行する"""
    # データ取得と指標計算
    df = await get_historical_data(symbol, TIMEFRAME)
    if df is None or df.empty or len(df) < 50:
        return

    df, atr_val = calculate_indicators(df)
    
    # オーダーブックの分析
    book_data = await get_order_book_analysis(symbol)
    
    # シグナル生成
    signals = generate_signals(df, symbol, atr_val, dominance_trend, book_data)
    
    if signals:
        best_signal = max(signals, key=lambda x: x['score'])
        results_dict[symbol] = best_signal

# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version=BOT_VERSION)

@app.on_event("startup")
async def startup_event():
    logging.info(f"🚀 Apex BOT {BOT_VERSION} Startup initializing...") 
    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    # global EXCHANGE_CLIENT は、この関数内では再代入がないため省略可能だが、明示的に宣言を維持
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status():
    """ボットの稼働状況を確認するエンドポイント"""
    status_msg = {
        "status": "ok",
        "bot_version": BOT_VERSION,
        "current_client": CCXT_CLIENT_NAME.upper(),
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_analysis_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "signal_threshold": SIGNAL_THRESHOLD,
        "top_signal_count": TOP_SIGNAL_COUNT
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": f"Apex BOT {BOT_VERSION} is running."})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    # uvicorn.run(app, host="0.0.0.0", port=port) # main_renderから実行する場合の標準的な記述
    # Render環境での実行に合わせ、uvicorn.runはコメントアウトまたは実行環境に依存する形で設定
    pass
