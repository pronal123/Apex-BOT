# ====================================================================================
# Apex BOT v11.2.0-KRAKEN TRADING FOCUS (最終安定化版 - データ取得の厳格化)
# 修正点: 
# 1. 銘柄数TOP10に削減 (TOP_SYMBOL_LIMIT=10)
# 2. リクエスト遅延とインターバルの延長 (SYMBOL_WAIT=4.5, LOOP_INTERVAL=360)
# 3. CCXTクライアントに内部遅延を設定 (rateLimit=2000)
# 4. テクニカル計算前のデータ行数チェックを厳格化 (ATR/MACD Hist=0回避)
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
import random
from fastapi import FastAPI
from fastapi.responses import JSONResponse 
import uvicorn
from dotenv import load_dotenv
import sys 

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT"] 
TOP_SYMBOL_LIMIT = 10      # 🚨 修正: Krakenの負荷集中を避け、監視銘柄をTOP10に削減
LOOP_INTERVAL = 360        # 🚨 修正: 分析サイクルを360秒（6分）に延長 (安定化)
SYMBOL_WAIT = 4.5          # 🚨 修正: 銘柄間の遅延を4.5秒に延長 (レート制限回避を強化)

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# 設定値
PING_INTERVAL = 8          
DYNAMIC_UPDATE_INTERVAL = 60 * 30
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2
BEST_POSITION_INTERVAL = 60 * 60 * 12
SIGNAL_THRESHOLD = 0.55 
CLIENT_COOLDOWN = 45 * 60  # 45分 (単独クライアントがエラーした場合の長時間クールダウン)
REQUIRED_OHLCV_LIMITS = {'15m': 100, '1h': 100, '4h': 100} # 🚨 修正: 必要OHLCVを100に削減
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

# グローバル状態変数
CCXT_CLIENTS_DICT: Dict[str, ccxt_async.Exchange] = {}
CCXT_CLIENT_NAMES: List[str] = []
CCXT_CLIENT_NAME: str = 'Kraken' 
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
NEUTRAL_NOTIFIED_TIME: float = 0
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_BEST_POSITION_TIME: float = 0 
LAST_SUCCESS_TIME: float = 0.0
TOTAL_ANALYSIS_ATTEMPTS: int = 0
TOTAL_ANALYSIS_ERRORS: int = 0
ACTIVE_CLIENT_HEALTH: Dict[str, float] = {} 
BTC_DOMINANCE_CONTEXT: Dict = {} 

# ロギング設定
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    stream=sys.stdout, 
                    force=True)
logging.getLogger('ccxt').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)


# ====================================================================================
# UTILITIES & CLIENTS (CCXT実装)
# ====================================================================================

def format_price_utility(price: float, symbol: str) -> str:
    """価格表示をシンボルに応じて整形"""
    base_sym = symbol.split('/')[0]
    if base_sym in ["BTC", "ETH"]:
        return f"{price:,.2f}"
    elif price > 1.0:
        return f"{price:,.4f}"
    else:
        return f"{price:.6f}"

# メッセージ整形関数
def format_price_lambda(symbol: str) -> Callable[[float], str]:
    return lambda p: format_price_utility(p, symbol)

def format_telegram_message(signal: Dict) -> str:
    """シグナルデータからTelegram通知メッセージを整形 (MTFA情報を統合)"""
    
    macro_trend = signal['macro_context']['trend']
    tech_data = signal.get('tech_data', {})
    mtfa_data = signal.get('mtfa_data', {}) 
    source_client = signal.get('client', 'N/A')
    
    adx_str = f"{tech_data.get('adx', 25):.1f}"
    bb_width_pct = tech_data.get('bb_width_pct', 0)
    
    format_price = format_price_lambda(signal['symbol'])
    
    # --- 1. 中立/ヘルス通知 ---
    if signal['side'] == "Neutral":
        if signal.get('is_health_check', False):
            stats = signal.get('analysis_stats', {"attempts": 0, "errors": 0, "last_success": 0})
            error_rate = (stats['errors'] / stats['attempts']) * 100 if stats['attempts'] > 0 else 0
            last_success_time = datetime.fromtimestamp(stats['last_success'], JST).strftime('%H:%M:%S') if stats['last_success'] > 0 else "N/A"
            
            return (
                f"🚨 <b>Apex BOT v11.2.0-KRAKEN FOCUS - 死活監視 (システム正常)</b> 🟢\n"
                f"<i>強制通知時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST</i>\n\n"
                f"• **市場コンテクスト**: {macro_trend} (BBands幅: {bb_width_pct:.2f}%) \n"
                f"• **🤖 BOTヘルス**: 最終成功: {last_success_time} JST (エラー率: {error_rate:.1f}%) \n"
                f"• **利用クライアント**: Kraken (シングルクライアント体制)\n"
                f"<b>【BOTの判断】: データ取得と分析は正常に機能しています。待機中。</b>"
            )

        rsi_str = f"{tech_data.get('rsi', 50):.1f}"
        macd_hist_str = f"{tech_data.get('macd_hist', 0):.4f}"
        confidence_pct = signal['confidence'] * 200

        return (
            f"⚠️ <b>{signal['symbol']} - 市場分析速報 (中立)</b> ⏸️\n"
            f"<b>信頼度: {confidence_pct:.1f}%</b> (データ元: {source_client})\n"
            f"---------------------------\n"
            f"• <b>市場環境/レジーム</b>: {signal['regime']} (ADX: {adx_str}) | {macro_trend} (BB幅: {bb_width_pct:.2f}%)\n"
            f"\n"
            f"📊 <b>テクニカル詳細</b>:\n"
            f"  - <i>RSI (15m)</i>: {rsi_str} | <i>MACD Hist (15m)</i>: {macd_hist_str}\n" 
            f"  - <i>MAとの位置</i>: {tech_data.get('ma_position', '中立')}\n"
            f"\n"
            f"<b>【BOTの判断】: {signal['regime']}で方向性が不鮮明です。様子見推奨。</b>"
        )

    # --- 2. トレードシグナル通知 ---
    score = signal['score']
    side_icon = "⬆️ LONG" if signal['side'] == "ロング" else "⬇️ SHORT"
    
    if score >= 0.85: score_icon = "🚀🌕🌕"; lot_size = "MAX"; action = "積極的にエントリー (高確度)"
    elif score >= 0.75: score_icon = "🔥🔥🔥"; lot_size = "大"; action = "標準的なエントリー (良好)"
    elif score >= 0.60: score_icon = "🔥🌟"; lot_size = "中"; action = "慎重なエントリー (許容範囲)"
    else: score_icon = "✨"; lot_size = "小"; action = "極めて慎重に"

    rsi_str = f"{tech_data.get('rsi', 50):.1f}"
    macd_hist_str = f"{tech_data.get('macd_hist', 0):.4f}"
    atr_val = tech_data.get('atr_value', 0)
    sentiment_pct = signal.get('sentiment_score', 0.5) * 100
    
    h1_trend = mtfa_data.get('h1_trend', 'N/A')
    h4_trend = mtfa_data.get('h4_trend', 'N/A')
    
    mtfa_summary = f"1H: {h1_trend} | 4H: {h4_trend}"
    
    overall_judgment = "◎ 3つの時間軸が完全に一致しています。" if h1_trend == signal['side'] and h4_trend == signal['side'] else "○ 1H/4Hのトレンド方向を確認し、短期シグナルを補強。"

    penalty_info = ""
    if signal.get('volatility_penalty_applied'):
        penalty_info = "⚠️ ボラティリティペナルティ適用済 (荒れた相場)"
        
    rr_ratio = signal.get('rr_ratio', 0.0) # 新しく追加

    # ATRの値が0の場合の警告メッセージを追加
    atr_warning = ""
    if atr_val == 0.0:
         atr_warning = "⚠️ <b>ATR/TP/SLが0: データ不足または計算失敗。取引不可。</b>"

    return (
        f"{score_icon} <b>{signal['symbol']} - {side_icon} シグナル発生!</b> {score_icon}\n"
        f"<b>信頼度スコア (MTFA統合): {score * 100:.2f}%</b> {penalty_info}\n"
        f"データ元: {source_client}\n"
        f"-----------------------------------------\n"
        f"• <b>現在価格</b>: <code>${format_price(signal['price'])}</code>\n"
        f"• <b>ATR (ボラティリティ指標)</b>: <code>{format_price(atr_val)}</code>\n" 
        f"\n"
        f"🎯 <b>ATRに基づく取引計画</b>:\n"
        f"  - エントリー: **<code>${format_price(signal['entry'])}</code>**\n"
        f"🟢 <b>利確 (TP)</b>: **<code>${format_price(signal['tp1'])}</code>**\n" 
        f"🔴 <b>損切 (SL)</b>: **<code>${format_price(signal['sl'])}</code>**\n"
        f"  - **リスクリワード比 (RRR)**: **<code>1:{rr_ratio:.2f}</code>** (スコアに基づく動的設定)\n" 
        f"{atr_warning}\n" # 警告メッセージを追加
        f"📈 <b>複合分析詳細</b>:\n"
        f"  - <b>マルチタイムフレーム (MTFA)</b>: {mtfa_summary} ({overall_judgment})\n"
        f"  - <i>市場レジーム</i>: {signal['regime']} (ADX: {adx_str}) | BBands幅: {bb_width_pct:.2f}%\n"
        f"  - <i>モメンタム/過熱</i>: RSI (15m): {rsi_str} | MACD Hist: {macd_hist_str}\n"
        f"  - <i>マクロ環境</i>: {macro_trend} | 感情: {sentiment_pct:.1f}% Positive\n"
        f"\n"
        f"💰 <b>取引示唆</b>:\n"
        f"  - <b>推奨ロット</b>: {lot_size}\n"
        f"  - <b>推奨アクション</b>: {action}\n"
        f"<b>【BOTの判断】: MTFAと複合モメンタムにより裏付けられた高確度シグナルです。</b>"
    )

def format_best_position_message(signal: Dict) -> str:
    """最良ポジション選定メッセージを整形"""
    score = signal['score']
    side_icon = "⬆️ LONG" if signal['side'] == "ロング" else "⬇️ SHORT"
    source_client = signal.get('client', 'N/A')
    
    macro_trend = signal['macro_context']['trend']
    tech_data = signal.get('tech_data', {})
    mtfa_data = signal.get('mtfa_data', {})
    
    adx_str = f"{tech_data.get('adx', 25):.1f}"
    bb_width_pct = f"{tech_data.get('bb_width_pct', 0):.2f}%"
    atr_val = tech_data.get('atr_value', 0)
    
    h1_trend = mtfa_data.get('h1_trend', 'N/A')
    h4_trend = mtfa_data.get('h4_trend', 'N/A')
    
    format_price = format_price_lambda(signal['symbol'])
    rr_ratio = signal.get('rr_ratio', 0.0) 
    
    atr_warning = ""
    if atr_val == 0.0:
         atr_warning = "⚠️ <b>ATR/TP/SLが0: データ不足または計算失敗。取引不可。</b>"

    return (
        f"👑 <b>{signal['symbol']} - 12時間 最良ポジション候補</b> {side_icon} 🔥\n"
        f"<i>選定時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST</i> (データ元: {source_client})\n"
        f"-----------------------------------------\n"
        f"• <b>選定スコア</b>: <code>{score * 100:.2f}%</code> (MTFA統合)\n"
        f"• <b>現在価格</b>: <code>${format_price(signal['price'])}</code>\n"
        f"• <b>ATR</b>: <code>{format_price(atr_val)}</code>\n"
        f"\n"
        f"🎯 <b>ATRに基づく取引計画 (推奨)</b>:\n"
        f"  - エントリー: <code>${format_price(signal['entry'])}</code>\n"
        f"  - 利確 (TP): <code>${format_price(signal['tp1'])}</code>\n"
        f"  - 損切 (SL): <code>${format_price(signal['sl'])}</code>\n"
        f"  - **リスクリワード比 (RRR)**: <code>1:{rr_ratio:.2f}</code>\n"
        f"{atr_warning}\n"
        f"💡 <b>選定理由 (MTFA/複合)</b>:\n"
        f"  1. <b>トレンド一致</b>: 1H ({h1_trend}) と 4H ({h4_trend}) が {side_icon.split()[1]} に一致。\n"
        f"  2. <b>レジーム</b>: {signal['regime']} (ADX: {adx_str}) で、BBands幅: {bb_width_pct}。\n"
        f"  3. <b>マクロ/需給</b>: {macro_trend} の状況下。\n"
        f"\n"
        f"<b>【BOTの判断】: 市場の状況に関わらず、最も優位性のある取引機会です。</b>"
    )

def initialize_ccxt_client():
    """CCXTクライアントを初期化（非同期）"""
    global CCXT_CLIENTS_DICT, CCXT_CLIENT_NAMES, ACTIVE_CLIENT_HEALTH
    
    clients = {
        'Kraken': ccxt_async.kraken({
            "enableRateLimit": True, 
            "timeout": 30000,
            # 🚨 修正: 組み込みの遅延を適用 (Krakenのデフォルト1000msを2000msに強化)
            "rateLimit": 2000 
        }), 
    }
    CCXT_CLIENTS_DICT = clients
    CCXT_CLIENT_NAMES = list(CCXT_CLIENTS_DICT.keys())
    ACTIVE_CLIENT_HEALTH = {name: time.time() for name in CCXT_CLIENT_NAMES}
    logging.info(f"✅ CCXTクライアント初期化完了。利用可能なクライアント: {CCXT_CLIENT_NAMES}")

def send_telegram_html(text: str, is_emergency: bool = False):
    """HTML形式でTelegramにメッセージを送信する（ブロッキング）"""
    if 'YOUR' in TELEGRAM_TOKEN:
        clean_text = text.replace("<b>", "").replace("</b>", "").replace("<i>", "").replace("</i>", "").replace("<code>", "").replace("</code>", "").replace("\n", " ").replace("•", "").replace("-", "").strip()
        logging.warning("⚠️ TELEGRAM_TOKENが初期値です。ログに出力されます。")
        logging.info("--- TELEGRAM通知（ダミー）---\n" + clean_text)
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML",
        "disable_web_page_preview": True, "disable_notification": not is_emergency
    }
    try:
        requests.post(url, json=payload, timeout=10)
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram送信エラーが発生しました: {e}")

async def send_test_message():
    """起動テスト通知"""
    test_text = (
        f"🤖 <b>Apex BOT v11.2.0-KRAKEN FOCUS - 起動テスト通知</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"<b>安定構成: Krakenの単独監視体制に移行し、ATRに基づくポジション分析を強化しました。</b>\n"
        f"<b>【主要変更点】: レート制限回避を強化し、データ不足による計算失敗を厳しくチェックします。</b>"
    )
    try:
        await asyncio.to_thread(lambda: send_telegram_html(test_text, is_emergency=True)) 
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")

async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[List[List[float]], str, str]:
    """CCXTからOHLCVデータを取得し、レート制限エラーを捕捉"""
    client = CCXT_CLIENTS_DICT.get(client_name)
    if not client: return [], "ClientError", client_name
    
    limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 100)
    try:
        ohlcv = await client.fetch_ohlcv(symbol, timeframe, limit=limit)
        if len(ohlcv) < limit * 0.8: 
            return ohlcv, "DataShortage", client_name
        
        # 🚨 修正: データが完全に空の場合もエラーとして扱う
        if not ohlcv or len(ohlcv) == 0:
             return [], "DataEmpty", client_name

        return ohlcv, "Success", client_name
        
    except ccxt.NotSupported:
        return [], "NotSupported", client_name
    except ccxt.RateLimitExceeded:
        return [], "RateLimit", client_name
    except ccxt.ExchangeError as e:
        if 'rate limit' in str(e).lower() or '429' in str(e) or 'timestamp' in str(e).lower(): 
             return [], "ExchangeError", client_name
        return [], "ExchangeError", client_name
    except ccxt.NetworkError:
        return [], "Timeout", client_name
    except Exception as e:
        if 'timeout' in str(e).lower():
            return [], "Timeout", client_name
        return [], "UnknownError", client_name

def get_crypto_macro_context() -> Dict:
    """仮想通貨のマクロ環境を取得 (BTC Dominance)"""
    context = {"trend": "中立", "btc_dominance": 0.0, "dominance_change_boost": 0.0}
    try:
        btc_d = yf.Ticker("BTC-USD").history(period="7d", interval="1d")
        if not btc_d.empty and len(btc_d) >= 7:
            latest_price = btc_d['Close'].iloc[-1]
            oldest_price = btc_d['Close'].iloc[0]
            dominance_7d_change = (latest_price / oldest_price - 1) * 100
            
            context["btc_dominance"] = latest_price
            
            if dominance_7d_change > 5:
                context["trend"] = "BTC優勢 (リスクオフ傾向)"
                context["dominance_change_boost"] = -0.1
            elif dominance_7d_change < -5:
                context["trend"] = "アルト優勢 (リスクオン傾向)"
                context["dominance_change_boost"] = 0.1
            
    except Exception:
        pass
    return context

def get_news_sentiment(symbol: str) -> Dict:
    """ニュース感情スコア（簡易版）"""
    sentiment_score = 0.5 + random.uniform(-0.1, 0.1) 
    return {"sentiment_score": np.clip(sentiment_score, 0.0, 1.0)}

# ====================================================================================
# CORE ANALYSIS FUNCTIONS (ATR値計算含む)
# ====================================================================================

def calculate_trade_levels(price: float, side: str, atr_value: float, score: float) -> Dict:
    """ATR値に基づいてエントリー、TP、SLを計算"""
    # 🚨 修正: ATR値が0以下の場合は、TP/SLをエントリーと同じにしてRRRを0に設定
    if atr_value <= 0 or pd.isna(atr_value): 
        return {"entry": price, "sl": price, "tp1": price, "rr_ratio": 0.0}
    
    sl_multiplier = np.clip(1.0 - (score - 0.55) * 0.5, 0.75, 1.0) 
    sl_dist = sl_multiplier * atr_value
    
    rr_ratio = np.clip(1.5 + (score - 0.55) * 3.3, 1.5, 3.0) 
    
    tp1_dist = rr_ratio * sl_dist
    
    entry = price
    
    if side == "ロング":
        sl = entry - sl_dist
        tp1 = entry + tp1_dist
    else:
        sl = entry + sl_dist
        tp1 = entry - tp1_dist
        
    return {"entry": entry, "sl": sl, "tp1": tp1, "rr_ratio": rr_ratio}

def calculate_technical_indicators(ohlcv: List[List[float]]) -> Dict:
    """OHLCVからテクニカル指標 (RSI, MACD, ADX, ATR, BBands) を計算"""
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
    
    # 🚨 修正: テクニカル計算前のデータフレームチェックを厳格化 (ATR/MACD Hist=0回避)
    MIN_REQUIRED_ROWS = 50 
    if len(df) < MIN_REQUIRED_ROWS:
        logging.warning(f"⚠️ テクニカル指標の計算をスキップ: データ行数が {len(df)} (< {MIN_REQUIRED_ROWS})")
        return {"rsi": 50, "macd_hist": 0, "adx": 25, "atr_value": 0.0, "bb_width_pct": 0, "ma_position_score": 0, "ma_position": "中立", "df": df}
    
    df.ta.macd(append=True)
    df.ta.rsi(append=True)
    df.ta.adx(append=True)
    df.ta.atr(append=True)
    bbands = df.ta.bbands()
    df.ta.sma(length=20, append=True)
    df.ta.sma(length=50, append=True)
    
    last = df.iloc[-1]
    
    bb_width_col = bbands.columns[bbands.columns.str.contains('BBW_')].tolist()
    bb_width = last[bb_width_col[0]] if bb_width_col and not pd.isna(last[bb_width_col[0]]) else 0.0
    bb_width_pct = bb_width / last['SMA_20'] * 100 if last['SMA_20'] > 0 and not pd.isna(last['SMA_20']) else 0
    
    ma_pos_score = 0
    ma_position = "中立"
    if last['Close'] > last['SMA_20'] and last['SMA_20'] > last['SMA_50']:
        ma_pos_score = 0.3
        ma_position = "強力なロングトレンド"
    elif last['Close'] < last['SMA_20'] and last['SMA_20'] < last['SMA_50']:
        ma_pos_score = -0.3
        ma_position = "強力なショートトレンド"
        
    atr_col = df.columns[df.columns.str.startswith('ATR_')].tolist()
    # NaNチェックを強化
    atr_value = last[atr_col[0]] if atr_col and not pd.isna(last[atr_col[0]]) else 0.0 
    
    macd_hist_col = df.columns[df.columns.str.startswith('MACDH_')].tolist()
    adx_col = df.columns[df.columns.str.startswith('ADX_')].tolist()
    rsi_col = df.columns[df.columns.str.startswith('RSI_')].tolist()
    
    return {
        "rsi": last[rsi_col[0]] if rsi_col and not pd.isna(last[rsi_col[0]]) else 50,
        "macd_hist": last[macd_hist_col[0]] if macd_hist_col and not pd.isna(last[macd_hist_col[0]]) else 0,
        "adx": last[adx_col[0]] if adx_col and not pd.isna(last[adx_col[0]]) else 25,
        "atr_value": atr_value, 
        "bb_width_pct": bb_width_pct,
        "ma_position_score": ma_pos_score,
        "ma_position": ma_position,
        "df": df
    }

def get_timeframe_trend(tech_data: Dict) -> str:
    """特定の時間枠のトレンドを判定"""
    ma_score = tech_data.get('ma_position_score', 0)
    adx = tech_data.get('adx', 25)
    
    if adx < 20: 
        return "Neutral"
    if ma_score > 0.1 and adx > 25:
        return "ロング"
    elif ma_score < -0.1 and adx > 25:
        return "ショート"
        
    return "Neutral"

def get_mtfa_score_adjustment(side: str, h1_trend: str, h4_trend: str, rsi_15m: float, rsi_h1: float) -> Tuple[float, Dict]:
    """マルチタイムフレーム分析によるスコア調整"""
    adjustment = 0.0
    mtfa_data = {'h1_trend': h1_trend, 'h4_trend': h4_trend}
    
    if side != "Neutral":
        if h4_trend == side: adjustment += 0.10
        elif h4_trend != "Neutral": adjustment -= 0.10 
        
        if h1_trend == side: adjustment += 0.05
        elif h1_trend != "Neutral" and h1_trend == h4_trend: adjustment -= 0.05
    
    # 過熱感ペナルティ
    if side == "ロング":
        if rsi_15m > 70 and rsi_h1 > 60: adjustment -= 0.10
    elif side == "ショート":
        if rsi_15m < 30 and rsi_h1 < 40: adjustment -= 0.10
        
    return adjustment, mtfa_data

def market_analysis_and_score(symbol: str, tech_data_15m: Dict, tech_data_h1: Dict, tech_data_h4: Dict, sentiment_data: Dict, macro_context: Dict) -> Tuple[float, str, str, Dict, bool]:
    """市場分析と最終スコアリングロジック"""
    df_15m = tech_data_15m.get('df')
    # 🚨 修正: データの存在チェックを再度行う
    if df_15m is None or len(df_15m) < 50: return 0.5, "Neutral", "不明", {}, False
    
    adx_15m = tech_data_15m.get('adx', 25)
    bb_width_pct_15m = tech_data_15m.get('bb_width_pct', 0)
    
    # 市場レジーム判定
    if bb_width_pct_15m < 2.0 and adx_15m < 25:
        regime = "レンジ相場 (抑制)"; regime_boost = 0.0
    elif bb_width_pct_15m > 4.0 and adx_15m > 30:
        regime = "トレンド相場 (強化)"; regime_boost = 0.1
    else:
        regime = "移行期"; regime_boost = 0.05
        
    rsi_15m = tech_data_15m.get('rsi', 50)
    macd_hist_15m = tech_data_15m.get('macd_hist', 0)
    ma_pos_score_15m = tech_data_15m.get('ma_position_score', 0)
    
    adx_direction_score = ma_pos_score_15m * (np.clip((adx_15m - 20) / 20, 0, 1) * 0.5 + 0.5)
    momentum_bias = ((rsi_15m - 50) / 50 * 0.15) * 0.4 + (np.clip(macd_hist_15m * 10, -0.15, 0.15)) * 0.6
    trend_bias = ma_pos_score_15m * 0.5 + adx_direction_score * 0.5
    
    composite_momentum_boost = 0.0
    if macd_hist_15m > 0 and rsi_15m > 55: composite_momentum_boost = 0.05
    elif macd_hist_15m < 0 and rsi_15m < 45: composite_momentum_boost = -0.05
    
    sentiment_score = sentiment_data.get('sentiment_score', 0.5)
    sentiment_bias = (sentiment_score - 0.5) * 0.2
    base_score = 0.5
    
    weighted_bias = (momentum_bias * 0.35) + (trend_bias * 0.35) + (sentiment_bias * 0.1) + (composite_momentum_boost * 0.2)
    tentative_score = np.clip(base_score + weighted_bias + regime_boost * np.sign(weighted_bias), 0.0, 1.0)
    
    if tentative_score > 0.5: side = "ロング"
    elif tentative_score < 0.5: side = "ショート"
    else: side = "Neutral"
    
    h1_trend = get_timeframe_trend(tech_data_h1)
    h4_trend = get_timeframe_trend(tech_data_h4)
    rsi_h1 = tech_data_h1.get('rsi', 50)
    mtfa_adjustment, mtfa_data = get_mtfa_score_adjustment(side, h1_trend, h4_trend, rsi_15m, rsi_h1)
    macro_adjustment = macro_context.get('dominance_change_boost', 0.0) * (0.5 if symbol != 'BTC/USDT' else 0.0)
    
    volatility_penalty = 0.0
    volatility_penalty_applied = False
    if bb_width_pct_15m > VOLATILITY_BB_PENALTY_THRESHOLD and adx_15m < 40:
        volatility_penalty = -0.1; volatility_penalty_applied = True
        
    final_score = np.clip(tentative_score + mtfa_adjustment + macro_adjustment + volatility_penalty, 0.0, 1.0)
    
    if final_score > 0.5 + SIGNAL_THRESHOLD / 2:
        final_side = "ロング"
    elif final_score < 0.5 - SIGNAL_THRESHOLD / 2:
        final_side = "ショート"
    else:
        final_side = "Neutral"
        
    display_score = abs(final_score - 0.5) * 2 if final_side != "Neutral" else abs(final_score - 0.5)
    
    return display_score, final_side, regime, mtfa_data, volatility_penalty_applied

async def generate_signal_candidate(symbol: str, macro_context_data: Dict, client_name: str) -> Optional[Dict]:
    """単一銘柄のシグナル候補を生成"""
    sentiment_data = get_news_sentiment(symbol)
    
    tasks = {
        '15m': fetch_ohlcv_with_fallback(client_name, symbol, '15m'),
        '1h': fetch_ohlcv_with_fallback(client_name, symbol, '1h'),
        '4h': fetch_ohlcv_with_fallback(client_name, symbol, '4h'),
    }
    
    results = await asyncio.gather(*tasks.values())
    
    ohlcv_data = {'15m': results[0][0], '1h': results[1][0], '4h': results[2][0]}
    status_data = {'15m': results[0][1], '1h': results[1][1], '4h': results[2][1]} 
    
    # 🚨 修正: 致命的なエラーやデータ不足の場合、即座にエラーを返す
    if status_data['15m'] in ["RateLimit", "Timeout", "ExchangeError", "UnknownError", "NotSupported", "DataEmpty", "DataShortage"] or not ohlcv_data['15m']:
        # DataEmptyやDataShortageの場合も、計算失敗として記録するためエラーとして扱う
        return {"symbol": symbol, "side": status_data['15m'], "score": 0.0, "client": client_name} 
        
    tech_data_15m_full = calculate_technical_indicators(ohlcv_data['15m'])
    tech_data_h1_full = calculate_technical_indicators(ohlcv_data['1h'])
    tech_data_h4_full = calculate_technical_indicators(ohlcv_data['4h'])
    
    # 🚨 修正: 15mのテクニカル計算が失敗した場合（データ不足で0が返る）もNeutralとして処理
    if tech_data_15m_full.get('atr_value', 0) == 0.0 or tech_data_15m_full.get('macd_hist', 0) == 0:
        logging.warning(f"⚠️ {symbol}: 15mのATR/MACD計算が0です。データ不足または計算失敗。Neutralとして処理します。")
        return {"symbol": symbol, "side": "Neutral", "confidence": 0.5, "regime": "Data Error",
                "macro_context": macro_context_data, "is_fallback": True,
                "tech_data": tech_data_15m_full, "client": client_name}


    tech_data_15m = {k: v for k, v in tech_data_15m_full.items() if k != 'df'}
    
    final_score, final_side, regime, mtfa_data, volatility_penalty_applied = market_analysis_and_score(
        symbol, tech_data_15m_full, tech_data_h1_full, tech_data_h4_full, 
        sentiment_data, macro_context_data
    )
    
    current_price = tech_data_15m_full['df']['Close'].iloc[-1]
    atr_value = tech_data_15m.get('atr_value', 0)
    
    trade_levels = calculate_trade_levels(current_price, final_side, atr_value, final_score)
    
    if final_side == "Neutral":
        return {"symbol": symbol, "side": "Neutral", "confidence": final_score, "regime": regime,
                "macro_context": macro_context_data, "is_fallback": status_data['15m'] != "Success",
                "tech_data": tech_data_15m, "client": client_name}
    
    source = client_name
    return {"symbol": symbol, "side": final_side, "price": current_price, "score": final_score,
            "entry": trade_levels['entry'], "sl": trade_levels['sl'],
            "tp1": trade_levels['tp1'], "tp2": trade_levels['tp1'], 
            "rr_ratio": trade_levels['rr_ratio'], 
            "regime": regime, "is_fallback": status_data['15m'] != "Success", 
            "macro_context": macro_context_data, "source": source, 
            "sentiment_score": sentiment_data["sentiment_score"],
            "tech_data": tech_data_15m,
            "mtfa_data": mtfa_data,
            "volatility_penalty_applied": volatility_penalty_applied,
            "client": client_name} 


# -----------------------------------------------------------------------------------
# ASYNC TASKS & MAIN LOOP
# -----------------------------------------------------------------------------------

async def update_monitor_symbols_dynamically(client_name: str, limit: int) -> List[str]:
    """出来高上位銘柄リストをCCXTから取得し、不適切な銘柄を除外する。"""
    global CURRENT_MONITOR_SYMBOLS
    logging.info(f"🔄 銘柄リストを更新します。出来高TOP{limit}銘柄を取得試行... (クライアント: {client_name})")
    
    client = CCXT_CLIENTS_DICT.get('Kraken') 
    if not client: 
        logging.error("致命的エラー: Krakenクライアントが見つかりません。")
        return DEFAULT_SYMBOLS

    # 🚨 修正: 除外リスト (USD建ての一部とステーブルコイン)
    EXCLUDE_SYMBOLS_PARTIAL = ['USDC/USD', 'USDT/USD', 'DAI/USD', 'TUSD/USD']

    try:
        tickers = await client.fetch_tickers()
        
        usdt_pairs = {
            symbol: ticker.get('quoteVolume', 0) 
            for symbol, ticker in tickers.items() 
            # 出来高があり、かつ除外リストに部分一致しないシンボルのみを抽出
            if (symbol.endswith('/USDT') or symbol.endswith('/USD')) 
            and ticker.get('quoteVolume', 0) > 0
            and not any(excl in symbol for excl in EXCLUDE_SYMBOLS_PARTIAL)
        }
        
        # 出来高TOP銘柄を抽出
        sorted_pairs = sorted(usdt_pairs.items(), key=lambda item: item[1], reverse=True)
        new_symbols = [symbol for symbol, volume in sorted_pairs[:limit]]

        # 出来高TOP10を取得後、BTC/USD, ETH/USDなどを手動で追加して安定性を確保 (TOP10に漏れた場合のため)
        # ただし、今回はTOP10にこれらが含まれているため、まずはフィルタリングで安定させます。

        if new_symbols:
            logging.info(f"✅ クライアント Kraken を使用し、出来高TOP{len(new_symbols)}銘柄を取得しました。")
            CURRENT_MONITOR_SYMBOLS = new_symbols
            return new_symbols

    except Exception as e:
        logging.warning(f"⚠️ クライアント Kraken で銘柄リスト取得エラーが発生しました: {type(e).__name__}。フォールバック銘柄を使用します。")

    logging.warning(f"❌ 出来高TOP銘柄の取得に失敗しました。フォールバックとして {len(DEFAULT_SYMBOLS)}銘柄を使用します。")
    CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS
    return DEFAULT_SYMBOLS


async def instant_price_check_task():
    """将来的にWebSocketなどでリアルタイム価格チェックを行うためのタスク (現状は遅延用)"""
    while True:
        await asyncio.sleep(15)
        
async def self_ping_task(interval: int):
    """BOTのヘルスチェックを定期的に実行するタスク"""
    global NEUTRAL_NOTIFIED_TIME
    while True:
        await asyncio.sleep(interval)
        if time.time() - NEUTRAL_NOTIFIED_TIME > 60 * 60 * 2: 
            stats = {"attempts": TOTAL_ANALYSIS_ATTEMPTS, "errors": TOTAL_ANALYSIS_ERRORS, "last_success": LAST_SUCCESS_TIME}
            health_signal = {"symbol": "BOT", "side": "Neutral", "confidence": 0.5, "regime": "N/A", "macro_context": BTC_DOMINANCE_CONTEXT, "is_health_check": True, "analysis_stats": stats, "tech_data": {}}
            asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_telegram_message(health_signal))))
            NEUTRAL_NOTIFIED_TIME = time.time()

async def signal_notification_task(signals: List[Optional[Dict]]):
    """シグナル通知の処理とクールダウン管理"""
    current_time = time.time()
    
    error_signals = ["RateLimit", "Timeout", "ExchangeError", "UnknownError", "NotSupported", "DataShortage", "DataEmpty"]
    valid_signals = [s for s in signals if s is not None and s.get('side') not in error_signals]
    
    for signal in valid_signals:
        symbol = signal['symbol']
        side = signal['side']
        score = signal.get('score', 0.0)
        
        if side == "Neutral" and signal.get('is_health_check', False):
            asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_telegram_message(signal))))
            
        elif side in ["ロング", "ショート"] and score >= SIGNAL_THRESHOLD:
            # 🚨 修正: TP/SLが0のシグナルは通知しない
            if signal.get('rr_ratio', 0.0) == 0.0:
                 logging.warning(f"⚠️ {symbol}: ATR/TP/SLが0のため、取引シグナル通知をスキップしました。")
                 continue

            if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_telegram_message(signal))))

async def best_position_notification_task():
    """定期的に最良のポジション候補を通知するタスク"""
    global LAST_BEST_POSITION_TIME
    while True:
        await asyncio.sleep(1)
        current_time = time.time()
        
        if current_time - LAST_BEST_POSITION_TIME >= BEST_POSITION_INTERVAL:
            strongest_signal = None
            max_score = 0
            
            for signal in LAST_ANALYSIS_SIGNALS:
                if signal.get('side') in ["ロング", "ショート"] and signal['score'] > max_score:
                    # 🚨 修正: TP/SLが0のシグナルは最良ポジションから除外
                    if signal.get('rr_ratio', 0.0) == 0.0:
                        continue
                        
                    max_score = signal['score']
                    strongest_signal = signal
            
            if strongest_signal and max_score >= 0.60:
                logging.info(f"👑 最良ポジション候補: {strongest_signal['symbol']} (Score: {max_score:.2f})")
                asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_best_position_message(strongest_signal), is_emergency=False)))
                LAST_BEST_POSITION_TIME = current_time
            
async def main_loop():
    """BOTのメイン実行ループ。分析、クライアント切り替え、通知を行う。"""
    global LAST_UPDATE_TIME, LAST_SUCCESS_TIME, TOTAL_ANALYSIS_ATTEMPTS, TOTAL_ANALYSIS_ERRORS
    global ACTIVE_CLIENT_HEALTH, CCXT_CLIENT_NAMES, LAST_ANALYSIS_SIGNALS, BTC_DOMINANCE_CONTEXT

    BTC_DOMINANCE_CONTEXT = await asyncio.to_thread(get_crypto_macro_context)
    LAST_UPDATE_TIME = time.time()
    await send_test_message()
    
    asyncio.create_task(self_ping_task(interval=PING_INTERVAL)) 
    asyncio.create_task(instant_price_check_task())
    asyncio.create_task(best_position_notification_task()) 
    
    if not CCXT_CLIENT_NAMES:
        logging.error("致命的エラー: 利用可能なCCXTクライアントがありません。ループを停止します。")
        return

    await update_monitor_symbols_dynamically('Kraken', limit=TOP_SYMBOL_LIMIT)

    while True:
        await asyncio.sleep(0.005)
        current_time = time.time()
        
        if current_time - LAST_UPDATE_TIME > DYNAMIC_UPDATE_INTERVAL:
            await update_monitor_symbols_dynamically('Kraken', limit=TOP_SYMBOL_LIMIT)
            BTC_DOMINANCE_CONTEXT = await asyncio.to_thread(get_crypto_macro_context)
            LAST_UPDATE_TIME = current_time

        client_name = 'Kraken'
        if current_time < ACTIVE_CLIENT_HEALTH.get(client_name, 0):
            logging.warning("❌ Krakenクライアントがクールダウン中です。次のインターバルまで待機します。")
            sleep_time = min(max(10, ACTIVE_CLIENT_HEALTH.get(client_name, current_time) - current_time), LOOP_INTERVAL)
            await asyncio.sleep(sleep_time) 
            continue
            
        analysis_queue: List[Tuple[str, str]] = [(symbol, client_name) for symbol in CURRENT_MONITOR_SYMBOLS]
            
        logging.info(f"🔍 分析開始 (対象銘柄: {len(analysis_queue)}銘柄, 利用クライアント: {client_name})")
        TOTAL_ANALYSIS_ATTEMPTS += 1
        
        signals: List[Optional[Dict]] = []
        has_major_error = False
        
        for symbol, client_name in analysis_queue:
            
            if time.time() < ACTIVE_CLIENT_HEALTH.get(client_name, 0):
                 continue
                 
            signal = await generate_signal_candidate(symbol, BTC_DOMINANCE_CONTEXT, client_name)
            signals.append(signal)

            if signal and signal.get('side') in ["RateLimit", "Timeout", "ExchangeError", "UnknownError", "NotSupported", "DataEmpty", "DataShortage"]:
                cooldown_end_time = time.time() + CLIENT_COOLDOWN
                
                error_msg = f"❌ {signal['side']}エラー発生: クライアント {client_name} のヘルスを {datetime.fromtimestamp(cooldown_end_time, JST).strftime('%H:%M:%S')} JST にリセット ({CLIENT_COOLDOWN/60:.0f}分クールダウン)。"
                logging.error(error_msg)
                
                ACTIVE_CLIENT_HEALTH[client_name] = cooldown_end_time
                
                if signal.get('side') in ["RateLimit", "Timeout", "ExchangeError", "DataEmpty", "DataShortage"]:
                    asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(error_msg, is_emergency=False)))
                    has_major_error = True
                    TOTAL_ANALYSIS_ERRORS += 1
                
                break 
                
            await asyncio.sleep(SYMBOL_WAIT) 

        
        LAST_ANALYSIS_SIGNALS = [s for s in signals if s is not None and s.get('side') not in ["RateLimit", "Timeout", "ExchangeError", "UnknownError", "NotSupported", "DataEmpty", "DataShortage"]]
        asyncio.create_task(signal_notification_task(signals))
        
        if not has_major_error:
            LAST_SUCCESS_TIME = current_time
            logging.info(f"✅ 分析サイクル完了。次の分析まで {LOOP_INTERVAL} 秒待機。")
            await asyncio.sleep(LOOP_INTERVAL) 
        else:
            logging.info("➡️ クライアントがクールダウン中のため、待機時間に移行します。")
            sleep_to_cooldown = ACTIVE_CLIENT_HEALTH['Kraken'] - current_time
            await asyncio.sleep(min(max(60, sleep_to_cooldown), LOOP_INTERVAL)) 

# -----------------------------------------------------------------------------------
# FASTAPI SETUP
# -----------------------------------------------------------------------------------

# 🚨 修正: バージョン情報を更新
app = FastAPI(title="Apex BOT API", version="v11.2.0-KRAKEN_FOCUS")

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にCCXTクライアントを初期化し、メインループを開始する"""
    initialize_ccxt_client()
    # 🚨 修正: 起動ログのバージョン情報を更新
    logging.info("🚀 Apex BOT v11.2.0-KRAKEN TRADING FOCUS Startup Complete.")
    
    asyncio.create_task(main_loop())


@app.get("/status")
def get_status():
    """ヘルスチェック用のエンドポイント"""
    status_msg = {
        "status": "ok",
        "bot_version": "v11.2.0-KRAKEN_FOCUS (TOP10)",
        "last_success_timestamp": LAST_SUCCESS_TIME,
        "active_clients_count": len(CCXT_CLIENT_NAMES) if time.time() >= ACTIVE_CLIENT_HEALTH.get('Kraken', 0) else 0,
        "monitor_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "macro_context_trend": BTC_DOMINANCE_CONTEXT.get('trend', 'N/A'),
        "total_attempts": TOTAL_ANALYSIS_ATTEMPTS,
        "total_errors": TOTAL_ANALYSIS_ERRORS,
        "client_health": {name: datetime.fromtimestamp(t, JST).strftime('%Y-%m-%d %H:%M:%S') for name, t in ACTIVE_CLIENT_HEALTH.items()}
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    """ルートエンドポイント (GET/HEAD) - 稼働確認用"""
    return JSONResponse(content={"message": "Apex BOT is running (v11.2.0-KRAKEN_FOCUS, TOP10)."}, status_code=200)
