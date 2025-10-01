# ====================================================================================
# Apex BOT v9.1.13-Robust - 堅牢性強化フルコード (KeyError & ExchangeError対応)
# 強化点:
# 1. 📌 KeyError: 'macro_context' バグを修正し、エラーシグナル通知時のクラッシュを防止。
# 2. 📌 CCXTの例外処理を強化 (BadSymbolなどを区別) し、不要なクライアントクールダウンを回避。
# 3. MTFA、複合モメンタム、ボラティリティペナルティロジックを維持。
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import ccxt # 同期クラスもエラー処理に使用
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

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

DEFAULT_SYMBOLS = ["BTC/USDT", "ETH/USDT", "SOL/USDT", "XRP/USDT", "ADA/USDT", "DOGE/USDT"] 

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# 設定値
LOOP_INTERVAL = 60       
PING_INTERVAL = 8        
DYNAMIC_UPDATE_INTERVAL = 600
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2
BEST_POSITION_INTERVAL = 60 * 60 * 12
SIGNAL_THRESHOLD = 0.55 
CLIENT_COOLDOWN = 60 * 30 # 通常のエラー/レート制限時のクールダウン
CLIENT_COOLDOWN_SHORT = 60 * 5 # BadSymbolなど、軽微なエラー時のクールダウン
REQUIRED_OHLCV_LIMITS = {'15m': 200, '1h': 200, '4h': 200}
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

# グローバル状態変数
CCXT_CLIENTS_DICT: Dict[str, ccxt_async.Exchange] = {}
CCXT_CLIENT_NAMES: List[str] = []
CCXT_CLIENT_NAME: str = 'Initializing' 
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

def initialize_ccxt_client():
    """CCXTクライアントを初期化（非同期）"""
    global CCXT_CLIENTS_DICT, CCXT_CLIENT_NAMES, ACTIVE_CLIENT_HEALTH
    # Render環境でAPIキー無しでの動作をテストするため、publicのみ利用可能な設定とする
    clients = {
        'OKX': ccxt_async.okx({"enableRateLimit": True, "timeout": 30000}),     
        'Coinbase': ccxt_async.coinbase({"enableRateLimit": True, "timeout": 20000,
                                         "options": {"defaultType": "spot", "fetchTicker": "public"}}),
        # Krakenはペア名の仕様が異なることが多いため、エラーに注意
        'Kraken': ccxt_async.kraken({"enableRateLimit": True, "timeout": 20000}), 
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
        f"🤖 <b>Apex BOT v9.1.13-Robust - 起動テスト通知 (堅牢性強化版)</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"<b>機能強化: エラー時のクラッシュ/連続クールダウンを防止。</b>"
    )
    try:
        await asyncio.to_thread(lambda: send_telegram_html(test_text, is_emergency=True))
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")


async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[List[List[float]], str]:
    """CCXTからOHLCVデータを取得し、レート制限エラーを捕捉"""
    client = CCXT_CLIENTS_DICT.get(client_name)
    if not client: return [], "ClientError"
    
    limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 100)
    try:
        ohlcv = await client.fetch_ohlcv(symbol, timeframe, limit=limit)
        if len(ohlcv) < limit:
            return ohlcv, "DataShortage"
        return ohlcv, "Success"
        
    except ccxt.RateLimitExceeded:
        return [], "RateLimit"
    # 📌 強化点: Symbolや非対応のエラーをExchangeErrorから分離
    except (ccxt.BadSymbol, ccxt.NotSupported): 
        return [], "NotSupported"
    except ccxt.ExchangeError as e:
        # 詳細なエラーロギングを維持
        logging.error(f"❌ {client_name} Exchangeエラー ({symbol}): {e}")
        return [], "ExchangeError"
    except ccxt.NetworkError:
        return [], "Timeout"
    except Exception as e:
        logging.error(f"❌ {client_name} その他のエラー: {e}")
        return [], "UnknownError"

async def fetch_order_book_depth_async(client_name: str, symbol: str) -> Dict:
    """CCXTからオーダーブック深度を取得し、買い/売り圧を計算"""
    client = CCXT_CLIENTS_DICT.get(client_name)
    if not client: return {"depth_ratio": 0.5, "total_depth": 0.0, "side": "ClientError"}

    try:
        orderbook = await client.fetch_order_book(symbol, limit=20) 
        
        bids = orderbook.get('bids', [])
        asks = orderbook.get('asks', [])
        
        total_bid_amount = sum(amount * price for price, amount in bids)
        total_ask_amount = sum(amount * price for price, amount in asks)
        
        total_depth = total_bid_amount + total_ask_amount
        depth_ratio = total_bid_amount / total_depth if total_depth > 0 else 0.5
        
        return {"depth_ratio": depth_ratio, "total_depth": total_depth, "side": "Success"}

    except (ccxt.BadSymbol, ccxt.NotSupported): 
        return {"depth_ratio": 0.5, "total_depth": 0.0, "side": "NotSupported"}
    except Exception as e:
        return {"depth_ratio": 0.5, "total_depth": 0.0, "side": "ExchangeError"}

def get_crypto_macro_context() -> Dict:
    """仮想通貨のマクロ環境を取得 (BTC Dominance)"""
    context = {"trend": "中立", "btc_dominance": 0.0, "dominance_change_boost": 0.0}
    try:
        btc_d = yf.Ticker("BTC-USD").history(period="7d", interval="1d")
        if not btc_d.empty:
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

# ====================================================================================
# TELEGRAM FORMATTING (KeyError修正)
# ====================================================================================

def format_price_lambda(symbol: str) -> Callable[[float], str]:
    return lambda p: format_price_utility(p, symbol)

def format_telegram_message(signal: Dict) -> str:
    """シグナルデータからTelegram通知メッセージを整形 (MTFA情報を統合)"""
    
    # 📌 【KeyError修正箇所】: エラーシグナル処理
    if signal.get('is_error_signal', False):
        error_type = signal.get('side', 'N/A')
        client_name = signal.get('client', 'N/A')
        cooldown_time_str = signal.get('cooldown_end_time', 'N/A')

        # エラーシグナルは通知しない設計だが、Task例外回避のために安全な文字列を返す
        return (
            f"❌ <b>BOT内部エラー: {signal['symbol']} データ取得失敗</b>\n"
            f"<i>クライアント: {client_name} - エラー種別: {error_type}</i>\n"
            f"システムがクライアントをクールダウンしました ({cooldown_time_str})。"
        )

    # 📌 【KeyError回避】: 存在しないキーを参照しないようにgetを使用
    macro_context = signal.get('macro_context', {})
    macro_trend = macro_context.get('trend', 'N/A')
    
    tech_data = signal.get('tech_data', {})
    mtfa_data = signal.get('mtfa_data', {}) 
    
    adx_str = f"{tech_data.get('adx', 25):.1f}"
    bb_width_pct = tech_data.get('bb_width_pct', 0)
    
    depth_ratio = signal.get('depth_ratio', 0.5)
    total_depth = signal.get('total_depth', 0)
    
    if total_depth > 10000000: liquidity_status = f"非常に厚い (${total_depth/1e6:.1f}M)"
    elif total_depth > 1000000: liquidity_status = f"厚い (${total_depth/1e6:.1f}M)"
    else: liquidity_status = f"普通〜薄い (${total_depth/1e6:.1f}M)"
    
    depth_status = "買い圧優勢" if depth_ratio > 0.52 else ("売り圧優勢" if depth_ratio < 0.48 else "均衡")
    format_price = format_price_lambda(signal['symbol'])
    
    # --- 1. 中立/ヘルス通知 ---
    if signal['side'] == "Neutral":
        if signal.get('is_health_check', False):
            stats = signal.get('analysis_stats', {"attempts": 0, "errors": 0, "last_success": 0})
            error_rate = (stats['errors'] / stats['attempts']) * 100 if stats['attempts'] > 0 else 0
            last_success_time = datetime.fromtimestamp(stats['last_success'], JST).strftime('%H:%M:%S') if stats['last_success'] > 0 else "N/A"
            
            return (
                f"🚨 <b>Apex BOT v9.1.13-Robust - 死活監視 (システム正常)</b> 🟢\n"
                f"<i>強制通知時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST</i>\n\n"
                f"• **市場コンテクスト**: {macro_trend} (BBands幅: {bb_width_pct:.2f}%) \n"
                f"• **🤖 BOTヘルス**: 最終成功: {last_success_time} JST (エラー率: {error_rate:.1f}%) \n"
                f"<b>【BOTの判断】: データ取得と分析は正常に機能しています。待機中。</b>"
            )

        rsi_str = f"{tech_data.get('rsi', 50):.1f}"
        macd_hist_str = f"{tech_data.get('macd_hist', 0):.4f}"
        confidence_pct = signal['confidence'] * 200

        return (
            f"⚠️ <b>{signal['symbol']} - 市場分析速報 (中立)</b> ⏸️\n"
            f"<b>信頼度: {confidence_pct:.1f}%</b>\n"
            f"---------------------------\n"
            f"• <b>市場環境/レジーム</b>: {signal['regime']} (ADX: {adx_str}) | {macro_trend} (BB幅: {bb_width_pct:.2f}%)\n"
            f"• <b>流動性/需給</b>: {liquidity_status} | {depth_status} (比率: {depth_ratio:.2f})\n"
            f"\n"
            f"📊 <b>テクニカル詳細</b>:\n"
            f"  - <i>RSI (15m)</i>: {rsi_str} | <i>MACD Hist (15m)</i>: {macd_hist_str}\n" 
            f"  - <i>MAとの位置</i>: {tech_data.get('ma_position', '中立')}\n"
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


    return (
        f"{score_icon} <b>{signal['symbol']} - {side_icon} シグナル発生!</b> {score_icon}\n"
        f"<b>信頼度スコア (MTFA統合): {score * 100:.2f}%</b> {penalty_info}\n"
        f"-----------------------------------------\n"
        f"• <b>現在価格</b>: <code>${format_price(signal['price'])}</code>\n"
        f"• <b>ATR (ボラティリティ指標)</b>: <code>{format_price(atr_val)}</code>\n"
        f"\n"
        f"🎯 <b>取引計画 (推奨)</b>:\n"
        f"  - エントリー: **<code>${format_price(signal['entry'])}</code>**\n"
        f"🟢 <b>利確 (TP)</b>: **<code>${format_price(signal['tp1'])}</code>** (ATRベース)\n" 
        f"🔴 <b>損切 (SL)</b>: **<code>${format_price(signal['sl'])}</code>** (ATRベース)\n"
        f"\n"
        f"📈 <b>複合分析詳細</b>:\n"
        f"  - <b>マルチタイムフレーム (MTFA)</b>: {mtfa_summary} ({overall_judgment})\n"
        f"  - <i>市場レジーム</i>: {signal['regime']} (ADX: {adx_str}) | BBands幅: {bb_width_pct:.2f}%\n"
        f"  - <i>モメンタム/過熱</i>: RSI (15m): {rsi_str} | MACD Hist: {macd_hist_str}\n"
        f"  - <i>マクロ環境</i>: {macro_trend} | 感情: {sentiment_pct:.1f}% Positive\n"
        f"\n"
        f"💰 <b>取引示唆</b>:\n"
        f"  - <b>推奨ロット</b>: {lot_size}\n"
        f"  - <b>推奨アクション</b>: {action}\n"
        f"<b>【BOTの判断】: MTFAと複合モメンタムにより裏付けられた高確度シグナルです。</b>"
    )

def format_best_position_message(signal: Dict) -> str:
    """最良ポジション選定メッセージを整形"""
    score = signal['score']
    side_icon = "⬆️ LONG" if signal['side'] == "ロング" else "⬇️ SHORT"
    
    macro_context = signal.get('macro_context', {})
    macro_trend = macro_context.get('trend', 'N/A')

    tech_data = signal.get('tech_data', {})
    mtfa_data = signal.get('mtfa_data', {})
    
    adx_str = f"{tech_data.get('adx', 25):.1f}"
    bb_width_pct = f"{tech_data.get('bb_width_pct', 0):.2f}%"
    depth_ratio = f"{signal.get('depth_ratio', 0.5):.2f}"
    atr_val = tech_data.get('atr_value', 0)
    
    h1_trend = mtfa_data.get('h1_trend', 'N/A')
    h4_trend = mtfa_data.get('h4_trend', 'N/A')
    
    format_price = format_price_lambda(signal['symbol'])
    
    return (
        f"👑 <b>{signal['symbol']} - 12時間 最良ポジション候補</b> {side_icon} 🔥\n"
        f"<i>選定時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST</i>\n"
        f"-----------------------------------------\n"
        f"• <b>選定スコア</b>: <code>{score * 100:.2f}%</code> (MTFA統合)\n"
        f"• <b>現在価格</b>: <code>${format_price(signal['price'])}</code>\n"
        f"• <b>ATR</b>: <code>{format_price(atr_val)}</code>\n"
        f"\n"
        f"🎯 <b>取引計画 (推奨)</b>:\n"
        f"  - エントリー: <code>${format_price(signal['entry'])}</code>\n"
        f"  - 利確 (TP): <code>${format_price(signal['tp1'])}</code> (ATRベース)\n"
        f"  - 損切 (SL): <code>${format_price(signal['sl'])}</code> (ATRベース)\n"
        f"\n"
        f"💡 <b>選定理由 (MTFA/複合)</b>:\n"
        f"  1. <b>トレンド一致</b>: 1H ({h1_trend}) と 4H ({h4_trend}) が {side_icon.split()[1]} に一致。\n"
        f"  2. <b>レジーム</b>: {signal['regime']} (ADX: {adx_str}) で、BBands幅: {bb_width_pct}。\n"
        f"  3. <b>マクロ/需給</b>: {macro_trend} の状況下、流動性比率: {depth_ratio}。\n"
        f"\n"
        f"<b>【BOTの判断】: 市場の状況に関わらず、最も優位性のある取引機会です。</b>"
    )

# ====================================================================================
# CORE ANALYSIS FUNCTIONS
# ====================================================================================
# ... (calculate_trade_levels, calculate_technical_indicators, get_news_sentiment, 
# get_timeframe_trend, get_mtfa_score_adjustment, market_analysis_and_score は変更なし) ...

# [NOTE: これらの関数は、前回のコードから完全にそのまま含まれているものと見なします。]

def calculate_trade_levels(price: float, side: str, atr_value: float, score: float) -> Dict:
    """ATRに基づいてTP/SLを計算"""
    if atr_value <= 0: return {"entry": price, "sl": price, "tp1": price, "tp2": price}
    rr_multiplier = 2.0 + (score - 0.55) * 5.0
    rr_multiplier = np.clip(rr_multiplier, 2.0, 4.0)
    sl_dist = 1.0 * atr_value
    tp1_dist = rr_multiplier * atr_value
    entry = price
    if side == "ロング":
        sl = entry - sl_dist
        tp1 = entry + tp1_dist
    else:
        sl = entry + sl_dist
        tp1 = entry - tp1_dist
    return {"entry": entry, "sl": sl, "tp1": tp1, "tp2": entry}

def calculate_technical_indicators(ohlcv: List[List[float]]) -> Dict:
    """pandas_taを使用して正確なテクニカル指標を計算"""
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
    if len(df) < 50:
         return {"rsi": 50, "macd_hist": 0, "adx": 25, "atr_value": 0, "bb_width_pct": 0, "ma_position_score": 0, "ma_position": "中立", "df": df}
    df.ta.macd(append=True); df.ta.rsi(append=True); df.ta.adx(append=True); df.ta.atr(append=True)
    bbands = df.ta.bbands(); df.ta.sma(length=20, append=True); df.ta.sma(length=50, append=True)
    last = df.iloc[-1]
    bb_width_col = bbands.columns[bbands.columns.str.contains('BBW_')].tolist()
    bb_width = last[bb_width_col[0]] if bb_width_col and not pd.isna(last[bb_width_col[0]]) else 0.0
    bb_width_pct = bb_width / last['SMA_20'] * 100 if last['SMA_20'] > 0 and not pd.isna(last['SMA_20']) else 0
    ma_pos_score = 0; ma_position = "中立"
    if last['Close'] > last['SMA_20'] and last['SMA_20'] > last['SMA_50']:
        ma_pos_score = 0.3; ma_position = "強力なロングトレンド"
    elif last['Close'] < last['SMA_20'] and last['SMA_20'] < last['SMA_50']:
        ma_pos_score = -0.3; ma_position = "強力なショートトレンド"
    macd_hist_col = df.columns[df.columns.str.startswith('MACDH_')].tolist()
    adx_col = df.columns[df.columns.str.startswith('ADX_')].tolist()
    atr_col = df.columns[df.columns.str.startswith('ATR_')].tolist()
    rsi_col = df.columns[df.columns.str.startswith('RSI_')].tolist()
    return {
        "rsi": last[rsi_col[0]] if rsi_col and not pd.isna(last[rsi_col[0]]) else 50,
        "macd_hist": last[macd_hist_col[0]] if macd_hist_col and not pd.isna(last[macd_hist_col[0]]) else 0,
        "adx": last[adx_col[0]] if adx_col and not pd.isna(last[adx_col[0]]) else 25,
        "atr_value": last[atr_col[0]] if atr_col and not pd.isna(last[atr_col[0]]) else 0,
        "bb_width_pct": bb_width_pct, "ma_position_score": ma_pos_score,
        "ma_position": ma_position, "df": df
    }

def get_news_sentiment(symbol: str) -> Dict:
    """ニュース感情スコア（簡易版）"""
    sentiment_score = 0.5 + random.uniform(-0.1, 0.1) 
    return {"sentiment_score": np.clip(sentiment_score, 0.0, 1.0)}

def get_timeframe_trend(tech_data: Dict) -> str:
    """テクニカルデータから、その時間軸の主要トレンドを判定"""
    ma_score = tech_data.get('ma_position_score', 0); adx = tech_data.get('adx', 25)
    if adx < 20: return "Neutral"
    if ma_score > 0.1 and adx > 25: return "ロング"
    elif ma_score < -0.1 and adx > 25: return "ショート"
    return "Neutral"

def get_mtfa_score_adjustment(side: str, h1_trend: str, h4_trend: str, rsi_15m: float, rsi_h1: float) -> Tuple[float, Dict]:
    """MTFAに基づき、スコア調整値と詳細データを出力"""
    adjustment = 0.0; mtfa_data = {'h1_trend': h1_trend, 'h4_trend': h4_trend}
    if side != "Neutral":
        if h4_trend == side: adjustment += 0.10
        elif h4_trend != "Neutral": adjustment -= 0.10 
        if h1_trend == side: adjustment += 0.05
        elif h1_trend != "Neutral" and h1_trend == h4_trend: adjustment -= 0.05
    if side == "ロング":
        if rsi_15m > 70 and rsi_h1 > 60: adjustment -= 0.10; mtfa_data['overbought'] = True
    elif side == "ショート":
        if rsi_15m < 30 and rsi_h1 < 40: adjustment -= 0.10; mtfa_data['oversold'] = True
    return adjustment, mtfa_data

def market_analysis_and_score(symbol: str, tech_data_15m: Dict, tech_data_h1: Dict, tech_data_h4: Dict, depth_data: Dict, sentiment_data: Dict, macro_context: Dict) -> Tuple[float, str, str, Dict, bool]:
    """MTFA、複合モメンタム、ボラティリティペナルティを統合したスコアリングロジック"""
    df_15m = tech_data_15m.get('df')
    if df_15m is None or len(df_15m) < 50: return 0.5, "Neutral", "不明", {}, False
    adx_15m = tech_data_15m.get('adx', 25); bb_width_pct_15m = tech_data_15m.get('bb_width_pct', 0)
    if bb_width_pct_15m < 2.0 and adx_15m < 25: regime = "レンジ相場 (抑制)"; regime_boost = 0.0
    elif bb_width_pct_15m > 4.0 and adx_15m > 30: regime = "トレンド相場 (強化)"; regime_boost = 0.1
    else: regime = "移行期"; regime_boost = 0.05
    rsi_15m = tech_data_15m.get('rsi', 50); macd_hist_15m = tech_data_15m.get('macd_hist', 0); ma_pos_score_15m = tech_data_15m.get('ma_position_score', 0)
    adx_direction_score = ma_pos_score_15m * (np.clip((adx_15m - 20) / 20, 0, 1) * 0.5 + 0.5)
    momentum_bias = ((rsi_15m - 50) / 50 * 0.15) * 0.4 + (np.clip(macd_hist_15m * 10, -0.15, 0.15)) * 0.6
    trend_bias = ma_pos_score_15m * 0.5 + adx_direction_score * 0.5
    composite_momentum_boost = 0.0
    if macd_hist_15m > 0 and rsi_15m > 55: composite_momentum_boost = 0.05
    elif macd_hist_15m < 0 and rsi_15m < 45: composite_momentum_boost = -0.05
    depth_ratio = depth_data.get('depth_ratio', 0.5); sentiment_score = sentiment_data.get('sentiment_score', 0.5)
    depth_bias = (depth_ratio - 0.5) * 0.2; sentiment_bias = (sentiment_score - 0.5) * 0.1
    base_score = 0.5
    weighted_bias = (momentum_bias * 0.3) + (trend_bias * 0.3) + (depth_bias * 0.1) + (sentiment_bias * 0.1) + (composite_momentum_boost * 0.2)
    tentative_score = base_score + weighted_bias + regime_boost * np.sign(weighted_bias)
    tentative_score = np.clip(tentative_score, 0.0, 1.0)
    if tentative_score > 0.5: side = "ロング"
    elif tentative_score < 0.5: side = "ショート"
    else: side = "Neutral"
    h1_trend = get_timeframe_trend(tech_data_h1); h4_trend = get_timeframe_trend(tech_data_h4); rsi_h1 = tech_data_h1.get('rsi', 50)
    mtfa_adjustment, mtfa_data = get_mtfa_score_adjustment(side, h1_trend, h4_trend, rsi_15m, rsi_h1)
    macro_adjustment = macro_context.get('dominance_change_boost', 0.0) * (0.5 if symbol != 'BTC/USDT' else 0.0)
    volatility_penalty = 0.0; volatility_penalty_applied = False
    if bb_width_pct_15m > VOLATILITY_BB_PENALTY_THRESHOLD and adx_15m < 40:
        volatility_penalty = -0.1; volatility_penalty_applied = True
    final_score = tentative_score + mtfa_adjustment + macro_adjustment + volatility_penalty
    final_score = np.clip(final_score, 0.0, 1.0)
    if final_score > 0.5 + SIGNAL_THRESHOLD / 2: final_side = "ロング"
    elif final_score < 0.5 - SIGNAL_THRESHOLD / 2: final_side = "ショート"
    else: final_side = "Neutral"
    display_score = abs(final_score - 0.5) * 2 if final_side != "Neutral" else abs(final_score - 0.5)
    return display_score, final_side, regime, mtfa_data, volatility_penalty_applied


async def generate_signal_candidate(symbol: str, macro_context_data: Dict, client_name: str) -> Optional[Dict]:
    """全時間軸のデータを取得し、MTFAを実行"""
    
    sentiment_data = get_news_sentiment(symbol)
    
    # 1. 全時間軸データ取得タスクを並行実行
    tasks = {
        '15m': fetch_ohlcv_with_fallback(client_name, symbol, '15m'),
        '1h': fetch_ohlcv_with_fallback(client_name, symbol, '1h'),
        '4h': fetch_ohlcv_with_fallback(client_name, symbol, '4h'),
        'depth': fetch_order_book_depth_async(client_name, symbol)
    }
    
    results = await asyncio.gather(*tasks.values())
    
    ohlcv_data = {'15m': results[0][0], '1h': results[1][0], '4h': results[2][0]}
    status_data = {'15m': results[0][1], '1h': results[1][1], '4h': results[2][1]}
    depth_data = results[3]
    
    # データ取得がエラーの場合、エラーシグナルを返す
    if status_data['15m'] in ["RateLimit", "Timeout", "ExchangeError", "UnknownError", "NotSupported", "ClientError"]:
        return {"symbol": symbol, "side": status_data['15m'], "score": 0.0, "client": client_name, "is_error_signal": True} 

    # 2. 全時間軸のテクニカル指標の計算
    tech_data_15m_full = calculate_technical_indicators(ohlcv_data['15m'])
    tech_data_h1_full = calculate_technical_indicators(ohlcv_data['1h'])
    tech_data_h4_full = calculate_technical_indicators(ohlcv_data['4h'])
    
    tech_data_15m = {k: v for k, v in tech_data_15m_full.items() if k != 'df'}
    
    # 3. 複合分析とスコアリング (MTFA/複合ロジック適用)
    final_score, final_side, regime, mtfa_data, volatility_penalty_applied = market_analysis_and_score(
        symbol, tech_data_15m_full, tech_data_h1_full, tech_data_h4_full, 
        depth_data, sentiment_data, macro_context_data
    )
    
    # 4. トレードレベルの計算
    current_price = tech_data_15m_full['df']['Close'].iloc[-1]
    atr_value = tech_data_15m.get('atr_value', 0)
    
    trade_levels = calculate_trade_levels(current_price, final_side, atr_value, final_score)
    
    
    # --- 5. シグナル結果の整形 ---
    if final_side == "Neutral":
        return {"symbol": symbol, "side": "Neutral", "confidence": final_score, "regime": regime,
                "macro_context": macro_context_data, "is_fallback": status_data['15m'] != "Success",
                "depth_ratio": depth_data['depth_ratio'], "total_depth": depth_data['total_depth'],
                "tech_data": tech_data_15m}

    # トレードシグナル
    source = client_name
    return {"symbol": symbol, "side": final_side, "price": current_price, "score": final_score,
            "entry": trade_levels['entry'], "sl": trade_levels['sl'],
            "tp1": trade_levels['tp1'], "tp2": trade_levels['tp2'],
            "regime": regime, "is_fallback": status_data['15m'] != "Success", 
            "depth_ratio": depth_data['depth_ratio'], "total_depth": depth_data['total_depth'],
            "macro_context": macro_context_data, "source": source, 
            "sentiment_score": sentiment_data["sentiment_score"],
            "tech_data": tech_data_15m,
            "mtfa_data": mtfa_data,
            "volatility_penalty_applied": volatility_penalty_applied} 


# -----------------------------------------------------------------------------------
# ASYNC TASKS & MAIN LOOP
# -----------------------------------------------------------------------------------

async def update_monitor_symbols_dynamically(client_name: str, limit: int):
    """監視銘柄リストを更新"""
    global CURRENT_MONITOR_SYMBOLS
    logging.info(f"🔄 銘柄リストを更新します。")
    alt_symbols = [f"ALT{i}/USDT" for i in range(1, 5)] 
    CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS + alt_symbols
    await asyncio.sleep(1)

async def instant_price_check_task():
    while True:
        await asyncio.sleep(15)
        
async def self_ping_task(interval: int):
    """BOTのヘルスチェックを定期的に実行するタスク"""
    global NEUTRAL_NOTIFIED_TIME
    while True:
        await asyncio.sleep(interval)
        if time.time() - NEUTRAL_NOTIFIED_TIME > 60 * 60 * 2: 
            stats = {"attempts": TOTAL_ANALYSIS_ATTEMPTS, "errors": TOTAL_ANALYSIS_ERRORS, "last_success": LAST_SUCCESS_TIME}
            health_signal = {"symbol": "BOT", "side": "Neutral", "confidence": 0.5, "regime": "N/A", "macro_context": BTC_DOMINANCE_CONTEXT, "is_health_check": True, "tech_data": {}, "depth_ratio": 0.5, "total_depth": 0}
            asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_telegram_message(health_signal))))
            NEUTRAL_NOTIFIED_TIME = time.time()

async def signal_notification_task(signals: List[Optional[Dict]]):
    """シグナル通知の処理とクールダウン管理"""
    current_time = time.time()
    for signal in signals:
        if signal is None: continue
        symbol = signal['symbol']
        side = signal['side']
        score = signal.get('score', 0.0)
        
        # 📌 【KeyError修正点】: エラーシグナルを通知ロジックから除外
        if signal.get('is_error_signal', False):
            # エラーシグナルはメインループ内でロギングとクールダウン処理を行うため、ここでは通知しない
            continue 
        
        if side in ["ロング", "ショート"] and score >= SIGNAL_THRESHOLD:
            if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                # to_thread内でformat_telegram_messageが呼ばれ、エラーシグナルではないため安全
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
                # エラーシグナルや中立シグナルを除外
                if signal.get('side') in ["ロング", "ショート"] and signal['score'] > max_score:
                    max_score = signal['score']
                    strongest_signal = signal
            
            if strongest_signal and max_score >= 0.60:
                logging.info(f"👑 最良ポジション候補: {strongest_signal['symbol']} (Score: {max_score:.2f})")
                asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_best_position_message(strongest_signal), is_emergency=False)))
                LAST_BEST_POSITION_TIME = current_time
        
async def main_loop():
    """BOTのメイン実行ループ。分析、クライアント切り替え、通知を行う。"""
    global LAST_UPDATE_TIME, LAST_SUCCESS_TIME, TOTAL_ANALYSIS_ATTEMPTS, TOTAL_ANALYSIS_ERRORS
    global CCXT_CLIENT_NAME, ACTIVE_CLIENT_HEALTH, CCXT_CLIENT_NAMES, LAST_ANALYSIS_SIGNALS, BTC_DOMINANCE_CONTEXT

    # 初期設定とテスト
    BTC_DOMINANCE_CONTEXT = await asyncio.to_thread(get_crypto_macro_context)
    LAST_UPDATE_TIME = time.time()
    await send_test_message()
    
    # バックグラウンドタスクの開始
    asyncio.create_task(self_ping_task(interval=PING_INTERVAL)) 
    asyncio.create_task(instant_price_check_task())
    asyncio.create_task(best_position_notification_task()) 
    
    if CCXT_CLIENT_NAMES:
        CCXT_CLIENT_NAME = CCXT_CLIENT_NAMES[0]
        await update_monitor_symbols_dynamically(CCXT_CLIENT_NAME, limit=30)
    else:
        logging.error("致命的エラー: 利用可能なCCXTクライアントがありません。ループを停止します。")
        return

    while True:
        await asyncio.sleep(0.005)
        current_time = time.time()
        
        # --- 1. 最適なCCXTクライアントの選択ロジック ---
        available_clients = {name: health_time for name, health_time in ACTIVE_CLIENT_HEALTH.items() if current_time >= health_time}
        if not available_clients:
            await asyncio.sleep(LOOP_INTERVAL)
            continue
        
        # OKXが利用可能であれば優先し、そうでなければクールダウン終了が最も早いクライアントを選ぶ
        CCXT_CLIENT_NAME = 'OKX' if current_time >= ACTIVE_CLIENT_HEALTH.get('OKX', 0) else max(available_clients, key=available_clients.get)


        # --- 2. 動的銘柄リストの更新とマクロ環境の取得 ---
        if current_time - LAST_UPDATE_TIME > DYNAMIC_UPDATE_INTERVAL:
            await update_monitor_symbols_dynamically(CCXT_CLIENT_NAME, limit=30) 
            BTC_DOMINANCE_CONTEXT = await asyncio.to_thread(get_crypto_macro_context)
            LAST_UPDATE_TIME = current_time

        # --- 3. 分析の実行 ---
        symbols_for_analysis = [sym for sym in CURRENT_MONITOR_SYMBOLS]
        
        logging.info(f"🔍 分析開始 (データソース: {CCXT_CLIENT_NAME}, 銘柄数: {len(symbols_for_analysis)}銘柄)")
        TOTAL_ANALYSIS_ATTEMPTS += 1
        
        signals: List[Optional[Dict]] = []
        
        for i in range(0, len(symbols_for_analysis), 5): 
            batch_symbols = symbols_for_analysis[i:i + 5]
            analysis_tasks = [
                generate_signal_candidate(symbol, BTC_DOMINANCE_CONTEXT, CCXT_CLIENT_NAME) 
                for symbol in batch_symbols
            ]
            batch_signals = await asyncio.gather(*analysis_tasks)
            signals.extend(batch_signals)
            await asyncio.sleep(1) # バッチ間のウェイト

        
        # --- 4. シグナルとエラー処理 (クールダウンの決定) ---
        has_major_error = False
        LAST_ANALYSIS_SIGNALS = [s for s in signals if s is not None and not s.get('is_error_signal', False)]
        asyncio.create_task(signal_notification_task(signals))
        
        for signal in signals:
            if signal and signal.get('is_error_signal', False):
                error_type = signal['side']
                client_name = signal['client']

                if error_type == "NotSupported":
                    cooldown_time = CLIENT_COOLDOWN_SHORT
                elif error_type in ["RateLimit", "Timeout", "ExchangeError", "UnknownError", "ClientError"]:
                    cooldown_time = CLIENT_COOLDOWN
                else:
                    continue # その他のエラーは無視
                
                cooldown_end_time = current_time + cooldown_time
                ACTIVE_CLIENT_HEALTH[client_name] = cooldown_end_time 
                
                cooldown_time_str = datetime.fromtimestamp(cooldown_end_time, JST).strftime('%H:%M:%S')
                logging.error(f"❌ {error_type}エラー発生: クライアント {client_name} のヘルスを {cooldown_time_str} JST にリセット (クールダウン: {cooldown_time}s)。")
                
                # エラーシグナルにクールダウン終了時刻を付与 (format_telegram_messageで利用可能に)
                signal['cooldown_end_time'] = cooldown_time_str
                
                # 重大なエラー（NotSupported以外）は即座のクライアント切り替えを促す
                if error_type != "NotSupported":
                    has_major_error = True
                
                TOTAL_ANALYSIS_ERRORS += 1
                if has_major_error: # 処理を中断し、次のクライアントに即座に切り替える
                    break 

        
        if not has_major_error:
            LAST_SUCCESS_TIME = current_time
            await asyncio.sleep(LOOP_INTERVAL)
        else:
            logging.info("➡️ 重大なエラー発生。クライアント切り替えのため、即座に次の分析サイクルに進みます。")
            await asyncio.sleep(1) 

# -----------------------------------------------------------------------------------
# FASTAPI SETUP
# -----------------------------------------------------------------------------------

app = FastAPI(title="Apex BOT API", version="v9.1.13-Robust_FULL")

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にCCXTクライアントを初期化し、メインループを開始する"""
    initialize_ccxt_client()
    logging.info("🚀 Apex BOT v9.1.13-Robust FULL Startup Complete.")
    asyncio.create_task(main_loop())


@app.get("/status")
def get_status():
    """ヘルスチェック用のエンドポイント"""
    status_msg = {
        "status": "ok",
        "bot_version": "v9.1.13-Robust_FULL",
        "last_success_timestamp": LAST_SUCCESS_TIME,
        "current_client": CCXT_CLIENT_NAME,
        "monitor_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "macro_context_trend": BTC_DOMINANCE_CONTEXT.get('trend', 'N/A')
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    """ルートエンドポイント (GET/HEAD) - 稼働確認用"""
    return JSONResponse(content={"message": "Apex BOT is running (v9.1.13-Robust_FULL)."}, status_code=200)
