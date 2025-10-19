# ====================================================================================
# Apex BOT v19.0.27 - Final Integrated Build (P16 FGI-Only)
#
# 最終統合設定:
# 1. 【マクロ分析】為替を無効化し、FGI（Fear & Greed Index）のみを分析に利用。
# 2. 【頻度向上】シグナル閾値を 0.75 → 0.68 に緩和。MACD/RSIボーナスを強化。
# 3. 【通知拡張】1時間ごとのレポートに最高スコア銘柄の「スコア内訳」を詳細表示。
# 4. 【ライブラリ】Yfinanceを完全に削除済み。
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
import sys
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

# 環境設定
JST = timezone(timedelta(hours=9))
BOT_VERSION = "v19.0.27 - Final Integrated Build (P16 FGI-Only)"
CCXT_CLIENT_NAME = os.getenv("CCXT_CLIENT_NAME", "mexc") # MEXCを使用
ANALYSIS_INTERVAL_SECONDS = 180 # メイン分析ループの実行間隔 (3分)
ANALYSIS_ONLY_INTERVAL = 3600 # 1時間ごとの定期通知 (3600秒)

# APIキー/トークン
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
MEXC_API_KEY = os.getenv("MEXC_API_KEY")
MEXC_SECRET = os.getenv("MEXC_SECRET")

# 出来高TOP30に加えて、主要な基軸通貨をDefaultに含めておく
DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "BNB/USDT", "XRP/USDT", "DOGE/USDT", "ADA/USDT"]

# FGI API
FGI_API_URL = "https://api.alternative.me/fng/?limit=1"

# スコアリング定数 (🚨 ユーザーカスタマイズ)
BASE_SCORE = 0.40                                  # 全ての分析の出発点となる基礎点
MIN_SIGNAL_SCORE = 0.68                            # 🚨 閾値緩和
LONG_TERM_TREND_PENALTY = -0.20                    # 4h SMA 50を下回る場合のペナルティ
MOMENTUM_MACD_BONUS = 0.20                         # 🚨 強化 (MACD)
MOMENTUM_RSI_BONUS = 0.15                          # 🚨 強化 (RSI)
VOLUME_CONFIRMATION_BONUS = 0.05                   # 出来高急増時のボーナス
LIQUIDITY_BONUS = 0.06                             # 買い板優位時のボーナス
FGI_LONG_BONUS = 0.07                              # 🚨 FGIボーナス (MAX値)
FGI_FEAR_THRESHOLD = 30                            # FGIがこの値以下の時、ボーナスを適用

# リスク/資金管理定数
MAX_RISK_CAPITAL_PERCENT = 0.01                    # 総資本に対する最大リスク割合 (1%)
MAX_RISK_AMOUNT_USD = 5.0                          # 1取引あたりの最大許容損失額 ($5.00)
ATR_SL_MULTIPLIER = 3.0                            # SLをATRの何倍にするか
MIN_RRR = 5.0                                      # 許容できる最低リスクリワード比率

# グローバル状態管理
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS
LAST_SUCCESS_TIME = 0.0
LAST_ANALYSIS_SIGNALS: List[Dict[str, Any]] = []
LAST_MACRO_CONTEXT: Dict[str, Any] = {}
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = 0.0

# ロギング設定
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s')

# FastAPIアプリの初期化
app = FastAPI()

# ====================================================================================
# UTILITY FUNCTIONS
# ====================================================================================

async def send_telegram_message(message: str):
    """Telegramにメッセージを送信する"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("⚠️ TelegramトークンまたはChat IDが設定されていません。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'Markdown',
    }
    
    try:
        # requestsライブラリは同期的であるため、asyncio.to_threadを使用して別スレッドで実行
        response = await asyncio.to_thread(requests.post, url, json=payload, timeout=5)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram送信エラー: {e}")

async def get_mexc_top_symbols(limit: int = 30) -> List[str]:
    """MEXCの出来高上位銘柄を取得する (静的リストを返す)"""
    # 実際にはCCXTのfetch_tickersなどを使用しますが、ここでは静的なリストを返す
    return DEFAULT_SYMBOLS[:limit]

async def fetch_ohlcv_with_fallback(exchange_name: str, symbol: str, timeframe: str) -> Tuple[pd.DataFrame, str, str]:
    """CCXTを使用してOHLCVデータを取得する"""
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT is None: return pd.DataFrame(), "Failure", "Client not initialized."
    
    try:
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe=timeframe, limit=200)
        if not ohlcv or len(ohlcv) < 50:
            return pd.DataFrame(), "Data Insufficient", f"Fetched only {len(ohlcv)} bars."
        
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        df.set_index('timestamp', inplace=True)
        return df, "Success", ""
        
    except Exception as e:
        return pd.DataFrame(), "Failure", str(e)

async def fetch_order_book_metrics(symbol: str) -> Tuple[float, float]:
    """オーダーブックの流動性（買い板/売り板の深さ）を計算する"""
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT is None: return 0.0, 0.0
    
    try:
        order_book = await EXCHANGE_CLIENT.fetch_order_book(symbol, limit=20)
        # 簡略化のため、ここでは単純に上位5件のボリューム合計として計算
        bid_volume = sum(amount for price, amount in order_book['bids'][:5])
        ask_volume = sum(amount for price, amount in order_book['asks'][:5])
        return bid_volume, ask_volume
        
    except Exception as e:
        return 0.0, 0.0

# ====================================================================================
# MACRO ANALYSIS (FGI ONLY)
# ====================================================================================

async def get_crypto_macro_context() -> Dict:
    """FGIのみを取得し、為替分析はスキップする"""
    
    fgi_score = 50 
    fgi_bonus = 0.0
    
    # 1. FGIの取得とボーナス計算
    try:
        # requestsライブラリは同期的であるため、asyncio.to_threadを使用して別スレッドで実行
        response = await asyncio.to_thread(requests.get, FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        fgi_score = int(data['data'][0]['value'])
        
        # FGIが恐怖レベル (30以下) の場合、ボーナスを適用
        if fgi_score <= FGI_FEAR_THRESHOLD:
             # FGIが低いほどロングの期待値が高いと判断し、MAXボーナスを適用
             fgi_bonus = FGI_LONG_BONUS # 0.07を適用
             
    except Exception as e:
        logging.warning(f"⚠️ FGI APIエラー。デフォルト値 50 (Neutral) を使用: {e}")
        fgi_score = 50 
        
    # 2. 為替は無効化 (ユーザー要望)
    forex_macro_status = "DISABLED"
    forex_bonus = 0.0
    
    macro_total_bonus = fgi_bonus + forex_bonus # FGIボーナスのみが残る
    
    return {
        "fgi_score": fgi_score,
        "fgi_bonus": fgi_bonus,
        "forex_macro_status": forex_macro_status,
        "forex_bonus": forex_bonus,
        "macro_total_bonus": macro_total_bonus
    }

# ====================================================================================
# CORE SCORING LOGIC
# ====================================================================================

def calculate_single_score(symbol: str, df_15m: pd.DataFrame, df_1h: pd.DataFrame, df_4h: pd.DataFrame, 
                           bid_volume: float, ask_volume: float, macro_context: Dict[str, Any]) -> Dict[str, Any]:
    """
    単一銘柄の統合スコアを計算し、その内訳(breakdown)も返します。
    """
    
    breakdown = {}
    
    # 1. 基礎スコアとマクロボーナス
    final_score = BASE_SCORE
    breakdown['base_score'] = BASE_SCORE
    
    # 💡 FGIボーナスが加算される
    macro_bonus = macro_context.get("macro_total_bonus", 0.0)
    final_score += macro_bonus
    breakdown['macro_bonus'] = macro_bonus

    # 2. 長期トレンド分析 (4H)
    df_4h['SMA_50'] = ta.sma(df_4h['close'], length=50)
    trend_penalty = 0.0
    if df_4h['close'].iloc[-1] < df_4h['SMA_50'].iloc[-1]:
        trend_penalty = LONG_TERM_TREND_PENALTY 
        final_score += trend_penalty
    breakdown['trend_penalty'] = trend_penalty
    
    # 3. モメンタム分析 (1H & 15M)
    df_1h.ta.macd(append=True)
    # MACDHカラム名を動的に取得 (MACD_12_26_9のMACDHなど)
    macd_h = df_1h.columns[df_1h.columns.str.contains('MACDH', case=False)].tolist()[-1]
    macd_bonus = 0.0
    # MACDHがプラスかつ増加している場合 (強気転換継続)
    if df_1h[macd_h].iloc[-1] > 0 and df_1h[macd_h].iloc[-1] > df_1h[macd_h].iloc[-2]:
        macd_bonus = MOMENTUM_MACD_BONUS 
        final_score += macd_bonus
    breakdown['macd_bonus'] = macd_bonus
    
    df_15m['RSI'] = ta.rsi(df_15m['close'], length=14)
    rsi_bonus = 0.0
    # RSIが売られすぎではないが、押し目圏(45以下)から反発し始めている場合
    if df_15m['RSI'].iloc[-1] < 45 and df_15m['RSI'].iloc[-1] > df_15m['RSI'].iloc[-2]:
        rsi_bonus = MOMENTUM_RSI_BONUS
        final_score += rsi_bonus
    breakdown['rsi_bonus'] = rsi_bonus
    
    # 4. 出来高確証 (15M)
    volume_bonus = 0.0
    avg_vol = df_15m['volume'].iloc[-20:-1].mean()
    current_vol = df_15m['volume'].iloc[-1]
    # 過去20バーの平均の2.5倍以上の出来高がある場合
    if current_vol > avg_vol * 2.5:
        volume_bonus = VOLUME_CONFIRMATION_BONUS 
        final_score += volume_bonus
    breakdown['volume_confirmation_bonus'] = volume_bonus

    # 5. 流動性優位 (オーダーブック)
    liquidity_bonus = 0.0
    # 買い板のボリュームが売り板の1.5倍以上ある場合
    if bid_volume > 0 and ask_volume > 0 and bid_volume / ask_volume > 1.5:
        liquidity_bonus = LIQUIDITY_BONUS
        final_score += liquidity_bonus
    breakdown['liquidity_bonus'] = liquidity_bonus
    
    # 6. リスクリワード比率 (SL/TPの計算)
    df_1h['ATR'] = ta.atr(df_1h['high'], df_1h['low'], df_1h['close'], length=14)
    atr = df_1h['ATR'].iloc[-1]
    entry_price = df_15m['close'].iloc[-1]
    sl_price = entry_price - (atr * ATR_SL_MULTIPLIER)
    risk_usd = entry_price - sl_price
    # 最小RRRに基づきTPを設定
    tp_price = entry_price + (risk_usd * MIN_RRR)
    
    result = {
        "symbol": symbol,
        "score": final_score,
        "entry_price": entry_price,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "risk_usd_per_unit": risk_usd,
        "atr_value": atr,
        "current_close_4h": df_4h['close'].iloc[-1],
        "breakdown": breakdown, 
    }
    
    return result

# ====================================================================================
# BALANCE & POSITION LOGIC
# ====================================================================================

async def fetch_current_balance_usdt() -> Tuple[float, str]:
    """USDT残高を取得し、ステータスを返す (MEXCパッチ適用)"""
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT is None: return 0.0, "CLIENT_ERROR"
    try:
        balance = await EXCHANGE_CLIENT.fetch_balance()
        usdt_total = balance['total'].get('USDT', 0.0)
        if usdt_total == 0.0: return 0.0, "ZERO_BALANCE"
        return float(usdt_total), "SUCCESS"
    except ccxt.AuthenticationError as e:
        if "700006" in str(e): return 0.0, "IP_RESTRICTED"
        return 0.0, "AUTH_ERROR"
    except Exception as e:
        return 0.0, "UNKNOWN_ERROR"

# ====================================================================================
# REPORTING & NOTIFICATION FUNCTIONS
# ====================================================================================

def format_integrated_analysis_message(signal_data: Dict[str, Any], balance: float) -> str:
    """取引シグナルが発生した場合のメッセージを整形する"""
    symbol = signal_data['symbol']
    score = signal_data['score']
    entry = signal_data['entry_price']
    sl = signal_data['sl_price']
    tp = signal_data['tp_price']
    risk_unit = signal_data['risk_usd_per_unit']
    max_risk = min(balance * MAX_RISK_CAPITAL_PERCENT, MAX_RISK_AMOUNT_USD)
    trade_size_unit = max_risk / risk_unit
    rrr = (tp - entry) / (entry - sl) if sl != entry else 0.0
    
    message = f"🚀 **Apex BOT 取引シグナル発生 (LONG)**\n\n"
    message += f"**銘柄:** `{symbol}`\n"
    message += f"**最終スコア:** `{score:.2f}` (閾値: {MIN_SIGNAL_SCORE:.2f})\n"
    message += f"**USDT残高:** ${balance:.2f}\n\n"
    message += f"--- **注文パラメータ** ---\n"
    message += f"🟢 **エントリー:** `{entry:,.8f}`\n"
    message += f"🔴 **ストップロス (SL):** `{sl:,.8f}`\n"
    message += f"🎯 **テイクプロフィット (TP):** `{tp:,.8f}`\n"
    message += f"**RRR:** `{rrr:.1f}x` | **許容リスク:** ${max_risk:.2f}\n"
    message += f"**注文サイズ:** `{trade_size_unit:,.4f}` 単位\n\n"
    message += f"💡 **分析サマリー:** スコアの内訳は定期レポートを参照してください。\n"
    return message


def format_analysis_only_message(
    fgi_score: int, 
    top_signal_data: Optional[Dict[str, Any]], 
    effective_signals: int,
    balance_status: str
) -> str:
    """1時間ごとの分析専用レポートのメッセージを整形し、トップスコア銘柄の詳細な内訳を含めます。"""
    
    message = f"🔔 **Apex BOT 定期分析レポート (1時間)**\n\n"
    message += f"**Bot ステータス:** `{balance_status}`\n"
    message += f"**有効シグナル総数:** {effective_signals} 件 (スコア {MIN_SIGNAL_SCORE:.2f} 以上)\n"
    
    # 💡 FGIのみを出力
    macro_status = "FEAR" if fgi_score <= FGI_FEAR_THRESHOLD else "NEUTRAL/GREED"
    message += f"**マクロ環境:** FGI: {fgi_score} ({macro_status}), 為替: DISABLED\n\n"

    # --- トップスコア銘柄の詳細 ---
    if top_signal_data and 'breakdown' in top_signal_data:
        symbol = top_signal_data.get('symbol', 'N/A')
        score = top_signal_data.get('score', 0.0)
        breakdown = top_signal_data['breakdown']
        
        message += "🏆 **最高スコア銘柄 (全30銘柄中):**\n"
        message += f"   - 銘柄: `{symbol}`\n"
        message += f"   - **最終スコア:** `{score:.2f}`\n\n"
        message += "--- **スコア内訳 (決定要因)** ---\n"
        
        plus_factors = []
        minus_factors = []
        # 内訳の表示順序を定義
        display_order = {
            'base_score': 'ベーススコア', 'macro_bonus': 'FGIボーナス', # FGIを上部に移動
            'trend_penalty': '長期トレンド(4h)',
            'macd_bonus': 'モメンタム(MACD)', 'rsi_bonus': '押し目(RSI)',
            'volume_confirmation_bonus': '出来高確証', 'liquidity_bonus': '流動性優位',
        }
        
        for key, display_key in display_order.items():
            value = breakdown.get(key, 0.0)
            
            # base_scoreは必ず表示
            if key == 'base_score':
                 plus_factors.append(f"  ⚪ {display_key}: +{value:.2f}")
                 continue

            # FGIボーナスが0でも、FGIが恐怖閾値を超えていなければ表示
            if key == 'macro_bonus':
                if value > 0:
                    plus_factors.append(f"  🟢 {display_key}: +{value:.2f} (FGI: {fgi_score})")
                else:
                    plus_factors.append(f"  ⚪ {display_key}: {value:.2f} (FGI: {fgi_score})")
                continue

            if value == 0.0: continue

            if value > 0: plus_factors.append(f"  🟢 {display_key}: +{value:.2f}")
            elif value < 0: minus_factors.append(f"  🔴 {display_key}: {value:.2f}")

        message += "📈 **プラス要因:**\n"
        if plus_factors: message += "\n".join(plus_factors) + "\n"
        else: message += "  (なし)\n"
            
        message += "\n📉 **マイナス要因:**\n"
        if minus_factors: message += "\n".join(minus_factors) + "\n"
        else: message += "  (なし)\n"

    else:
        message += "💡 **トップスコア銘柄:** N/A (分析データなし - 初回分析完了待ち)\n"

    message += "\n----------------------------------------\n"
    message += f"Bot Version: {BOT_VERSION}\n"
    
    return message


# ====================================================================================
# MAIN LOOPS
# ====================================================================================

async def main_loop():
    """メインの分析と取引実行ループ"""
    global LAST_SUCCESS_TIME, LAST_ANALYSIS_SIGNALS, CURRENT_MONITOR_SYMBOLS, LAST_MACRO_CONTEXT
    
    while True:
        start_time = time.time()
        
        # 1. 残高とステータスのチェック
        usdt_balance, balance_status = await fetch_current_balance_usdt()
        if balance_status != "SUCCESS" and balance_status != "ZERO_BALANCE":
            logging.error(f"🚨 取引スキップ: 残高取得ステータスが {balance_status} です。")
            await asyncio.sleep(ANALYSIS_INTERVAL_SECONDS)
            continue
            
        # 2. マクロ環境の取得 (FGI ONLY)
        LAST_MACRO_CONTEXT = await get_crypto_macro_context()
        
        # 3. モニター銘柄リストの更新
        monitor_symbols = await get_mexc_top_symbols(limit=30)
        
        logging.info(f"🔍 分析開始 (対象銘柄: {len(monitor_symbols)}, USDT残高: {usdt_balance:.2f}, ステータス: {balance_status}, FGI: {LAST_MACRO_CONTEXT['fgi_score']}, 為替: {LAST_MACRO_CONTEXT['forex_macro_status']})")
        
        all_signals: List[Dict[str, Any]] = []
        
        # 4. 各銘柄の分析とスコアリング (直列処理で代用)
        for symbol in monitor_symbols:
            df_15m, _, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, '15m')
            df_1h, _, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, '1h')
            df_4h, _, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, '4h')
            bid_vol, ask_vol = await fetch_order_book_metrics(symbol)
            
            if len(df_15m) > 0 and len(df_1h) > 0 and len(df_4h) > 0:
                signal_data = calculate_single_score(symbol, df_15m, df_1h, df_4h, bid_vol, ask_vol, LAST_MACRO_CONTEXT)
                all_signals.append(signal_data)
                
                # 5. シグナルチェックと取引実行
                if signal_data['score'] >= MIN_SIGNAL_SCORE and usdt_balance >= MAX_RISK_AMOUNT_USD:
                    logging.info(f"🎉 シグナル検知: {symbol} Score: {signal_data['score']:.2f}。取引を実行します。")
                    # 実際の取引実行ロジックはここに追加
                    
                    try:
                        message = format_integrated_analysis_message(signal_data, usdt_balance)
                        await send_telegram_message(message)
                    except Exception as e:
                        logging.error(f"取引シグナル通知エラー: {e}")

        # 6. グローバル変数の更新
        LAST_ANALYSIS_SIGNALS = all_signals
        effective_signals = len([s for s in all_signals if s['score'] >= MIN_SIGNAL_SCORE])

        logging.info(f"💡 分析完了 - 生成シグナル数 (全スコア): {effective_signals} 件")
        LAST_SUCCESS_TIME = time.time()
        
        end_time = time.time()
        elapsed = end_time - start_time
        
        wait_time = max(ANALYSIS_INTERVAL_SECONDS - elapsed, 1)
        logging.info(f"✅ 分析/取引サイクル完了 ({BOT_VERSION})。次の分析まで {wait_time:.0f} 秒待機。")

        await asyncio.sleep(wait_time)


async def analysis_only_notification_loop():
    """1時間ごとに、最高スコア銘柄の詳細分析レポートを送信する"""
    global LAST_ANALYSIS_ONLY_NOTIFICATION_TIME
    
    # 初回実行をすぐに実行
    LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time() - (ANALYSIS_ONLY_INTERVAL * 2) 
    
    while True:
        await asyncio.sleep(60) # 1分ごとチェック
        
        if (time.time() - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME) >= ANALYSIS_ONLY_INTERVAL:
            
            # 1. トップスコア銘柄の特定
            top_signal_data = None
            effective_signals = len([s for s in LAST_ANALYSIS_SIGNALS if s['score'] >= MIN_SIGNAL_SCORE])
            
            if LAST_ANALYSIS_SIGNALS:
                sorted_signals = sorted(
                    LAST_ANALYSIS_SIGNALS, 
                    key=lambda x: x['score'], 
                    reverse=True
                )
                top_signal_data = sorted_signals[0]
                
                logging.info(f"🔔 定期レポート生成: Top Score: {top_signal_data['symbol']} {top_signal_data['score']:.2f}")
            else:
                 logging.info(f"🔔 定期レポート生成: 分析データなし。")


            # 2. メッセージ生成と送信
            try:
                usdt_balance, balance_status = await fetch_current_balance_usdt()
                
                report_message = format_analysis_only_message(
                    LAST_MACRO_CONTEXT.get('fgi_score', 50), 
                    top_signal_data, 
                    effective_signals,
                    balance_status
                )
                
                logging.info("📧 分析レポートをTelegramへ送信します。")
                await send_telegram_message(report_message)
                
                logging.info(f"✅ 分析レポート送信完了。次の定期通知まで {ANALYSIS_ONLY_INTERVAL} 秒待機。")

                LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time()
                
            except Exception as e:
                logging.error(f"❌ 定期分析レポートの送信中にエラーが発生しました: {e}")

# ====================================================================================
# FASTAPI LIFECYCLE HOOKS
# ====================================================================================

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にCCXTクライアントを初期化し、ループを開始する"""
    global EXCHANGE_CLIENT, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME
    
    logging.info(f"🚀 Apex BOT {BOT_VERSION} Startup initializing...")
    
    # 1. CCXTクライアントの初期化
    try:
        if CCXT_CLIENT_NAME == 'mexc':
            EXCHANGE_CLIENT = ccxt_async.mexc({
                'apiKey': MEXC_API_KEY,
                'secret': MEXC_SECRET,
                'options': {'defaultType': 'spot'},
                'enableRateLimit': True, 
            })
            logging.info(f"CCXTクライアントを {CCXT_CLIENT_NAME.upper()} で初期化しました。(認証済み, Default: Spot)")
            
        else:
            raise ValueError(f"Unsupported exchange: {CCXT_CLIENT_NAME}")
            
    except Exception as e:
        logging.critical(f"❌ CCXTクライアント初期化失敗: {e}")
        sys.exit(1)

    # 2. ループの起動
    asyncio.create_task(main_loop())
    asyncio.create_task(analysis_only_notification_loop())
    
    logging.info("INFO: Application startup complete.")

@app.on_event("shutdown")
async def shutdown_event():
    """アプリケーション終了時にCCXTクライアントを閉じる"""
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/")
def home():
    """ヘルスチェックエンドポイント"""
    return JSONResponse(content={"status": "ok", "version": BOT_VERSION})
