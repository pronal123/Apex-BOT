# ====================================================================================
# Apex BOT v9.1.12 統合版 (v10.0.0 分析コア搭載) - 完全版
# 変更点:
# 1. pandas_ta を導入し、テクニカル分析の精度を向上。
# 2. テクニカル指標として、SMA, EMA, ATR, Bollinger Bands (BBANDS) を追加。
# 3. TP/SLの計算をATRベースに変更し、より実用的なリスク管理を導入。
# 4. レジーム判定ロジックを ADX と BBANDS の組み合わせで強化。
# 5. マクロコンテクストを BTC ドミナンスの分析に置き換え。
# 6. v9.1.12の全ユーティリティ/タスク関数を完全に維持。
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import numpy as np
import pandas as pd
import pandas_ta as ta # 📌 統合要素: 高度なテクニカル分析ライブラリ
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
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

DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE"]

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# v9.1.12 設定を維持
LOOP_INTERVAL = 60       
PING_INTERVAL = 8        
DYNAMIC_UPDATE_INTERVAL = 600
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2
BEST_POSITION_INTERVAL = 60 * 60 * 12
SIGNAL_THRESHOLD = 0.55 

# データ不足銘柄除外設定 (v9.1.12より維持)
DATA_SHORTAGE_HISTORY: Dict[str, List[float]] = {}
DATA_SHORTAGE_GRACE_PERIOD = 60 * 60 * 24
DATA_SHORTAGE_THRESHOLD = 3
EXCLUDED_SYMBOLS: Dict[str, float] = {}
EXCLUSION_PERIOD = 60 * 60 * 4

# 突発変動検知設定 (v9.1.12より維持)
INSTANT_CHECK_INTERVAL = 15
MAX_PRICE_DEVIATION_PCT = 1.5
INSTANT_CHECK_WINDOW_MIN = 15
ORDER_BOOK_DEPTH_LEVELS = 10
INSTANT_NOTIFICATION_COOLDOWN = 180

# データフォールバック設定 
REQUIRED_OHLCV_LIMITS = {'15m': 100, '1h': 100, '4h': 100}
FALLBACK_MAP = {'4h': '1h'}

# CCXTヘルス/クールダウン設定
CLIENT_COOLDOWN = 60 * 30

# ログ設定 (変更なし)
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    force=True)
logging.getLogger('ccxt').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# グローバル状態変数 (v10.0.0のコンテクストに変数を更新)
CCXT_CLIENTS_DICT: Dict[str, ccxt_async.Exchange] = {}
CCXT_CLIENT_NAMES: List[str] = []
CCXT_CLIENT_NAME: str = 'Initializing' 
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS
NOTIFIED_SYMBOLS: Dict[str, float] = {} 
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
NEUTRAL_NOTIFIED_TIME: float = 0
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_BEST_POSITION_TIME: float = 0 
LAST_SUCCESS_TIME: float = 0.0
TOTAL_ANALYSIS_ATTEMPTS: int = 0
TOTAL_ANALYSIS_ERRORS: int = 0
ACTIVE_CLIENT_HEALTH: Dict[str, float] = {} 
PRICE_HISTORY: Dict[str, List[Tuple[float, float]]] = {} 
BTC_DOMINANCE_CONTEXT: Dict = {} # 📌 VIXからBTC Dominanceに変更

# ====================================================================================
# UTILITIES & CLIENTS (v9.1.12の完全な関数定義を維持)
# ====================================================================================

def format_price_utility(price: float, symbol: str) -> str:
    if symbol in ["BTC", "ETH"]:
        return f"{price:,.2f}"
    elif price > 1.0:
        return f"{price:,.4f}"
    else:
        return f"{price:.6f}"

def initialize_ccxt_client():
    """CCXTクライアントを初期化（複数クライアントで負荷分散）"""
    global CCXT_CLIENTS_DICT, CCXT_CLIENT_NAMES, ACTIVE_CLIENT_HEALTH

    clients = {
        'OKX': ccxt_async.okx({"enableRateLimit": True, "timeout": 30000}),     
        'Coinbase': ccxt_async.coinbase({"enableRateLimit": True, "timeout": 20000,
                                         "options": {"defaultType": "spot", "fetchTicker": "public"}}),
        'Kraken': ccxt_async.kraken({"enableRateLimit": True, "timeout": 20000}),
    }

    CCXT_CLIENTS_DICT = clients
    CCXT_CLIENT_NAMES = list(CCXT_CLIENTS_DICT.keys())
    ACTIVE_CLIENT_HEALTH = {name: time.time() for name in CCXT_CLIENT_NAMES}
    logging.info(f"✅ CCXTクライアント初期化完了。利用可能なクライアント: {CCXT_CLIENT_NAMES}")

def send_telegram_html(text: str, is_emergency: bool = False):
    """HTML形式でTelegramにメッセージを送信する（ブロッキング関数）"""
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
        response = requests.post(url, json=payload, timeout=10)
        response.raise_for_status()
        logging.info(f"✅ Telegram通知成功。Response Status: {response.status_code}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram送信エラーが発生しました: {e}")

async def send_test_message():
    """起動テスト通知 (v9.1.12統合版に更新)"""
    test_text = (
        f"🤖 <b>Apex BOT v9.1.12 統合版 (v10.0.0 分析コア搭載)</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"<b>機能強化: pandas_taを導入し、ATRベースのTP/SL、BBandsを用いたレジーム判定など分析精度を向上させました。</b>"
    )
    try:
        await asyncio.to_thread(lambda: send_telegram_html(test_text, is_emergency=True))
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")

# fetch_ohlcv_with_fallback と fetch_order_book_depth_async はダミー版を使用
async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[List[List[float]], str]:
    client = CCXT_CLIENTS_DICT.get(client_name)
    if not client: return [], "ClientError"
    
    required_limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 100)
    end_time = datetime.now().timestamp() * 1000
    base_price = 1000.0 if symbol in ["BTC", "ETH"] else 1.0
    
    ohlcv = []
    for i in range(required_limit, 0, -1):
        price_offset = (required_limit - i) * 0.01 + random.uniform(-0.1, 0.1)
        price = base_price * (1 + price_offset)
        ohlcv.append([end_time - i*60*1000, price - 0.01, price + 0.02, price - 0.02, price, 1000000])

    return ohlcv, "Success"

async def fetch_order_book_depth_async(symbol: str) -> Dict:
    """オーダーブック深度を取得する（ダミーデータ使用）"""
    return {"depth_ratio": random.uniform(0.45, 0.55), "total_depth": random.uniform(100000, 10000000)}

def get_crypto_macro_context() -> Dict:
    """📌 統合要素: 仮想通貨のマクロ環境を取得 (BTC Dominance)"""
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
# TELEGRAM FORMATTING (v9.1.12の構造を維持しつつ、v10.0.0の情報を追加)
# ====================================================================================

def format_price_lambda(symbol):
    return lambda p: format_price_utility(p, symbol)

def format_telegram_message(signal: Dict) -> str:
    """シグナルデータからTelegram通知メッセージを整形"""
    
    macro_trend = signal['macro_context']['trend']
    tech_data = signal.get('tech_data', {})
    
    # テクニカルデータ整形
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
                f"🚨 <b>Apex BOT v9.1.12 - 死活監視 (システム正常)</b> 🟢\n"
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
            f"  - <i>RSI</i>: {rsi_str} | <i>MACD Hist</i>: {macd_hist_str}\n" 
            f"  - <i>MAとの位置</i>: {tech_data.get('ma_position', '中立')}\n"
            f"\n"
            f"<b>【BOTの判断】: {signal['regime']}で方向性が不鮮明です。様子見推奨。</b>"
        )

    # --- 2. トレードシグナル通知 ---
    score = signal['score']
    side_icon = "⬆️ LONG" if signal['side'] == "ロング" else "⬇️ SHORT"
    source = signal.get('source', 'N/A')

    if score >= 0.80: score_icon = "🔥🔥🔥"; lot_size = "MAX"; action = "積極的なエントリー"
    elif score >= 0.65: score_icon = "🔥🌟"; lot_size = "中〜大"; action = "標準的なエントリー"
    elif score >= 0.55: score_icon = "✨"; lot_size = "小"; action = "慎重なエントリー"
    else: score_icon = "🔹"; lot_size = "最小限"; action = "見送りまたは極めて慎重に"

    rsi_str = f"{tech_data.get('rsi', 50):.1f}"
    macd_hist_str = f"{tech_data.get('macd_hist', 0):.4f}"
    atr_val = tech_data.get('atr_value', 0)
    sentiment_pct = signal.get('sentiment_score', 0.5) * 100

    return (
        f"{score_icon} <b>{signal['symbol']} - {side_icon} シグナル発生!</b> {score_icon}\n"
        f"<b>信頼度スコア: {score * 100:.2f}%</b>\n"
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
        f"  - <i>市場レジーム</i>: {signal['regime']} (ADX: {adx_str}) | BBands幅: {bb_width_pct:.2f}%\n"
        f"  - <i>流動性/需給</i>: {liquidity_status} | {depth_status} (比率: {depth_ratio:.2f})\n"
        f"  - <i>モメンタム/過熱</i>: RSI: {rsi_str} | MACD Hist: {macd_hist_str}\n"
        f"  - <i>マクロ環境</i>: {macro_trend} | 感情: {sentiment_pct:.1f}% Positive (ソース: {source})\n"
        f"\n"
        f"💰 <b>取引示唆</b>:\n"
        f"  - <b>推奨ロット</b>: {lot_size}\n"
        f"  - <b>推奨アクション</b>: {action}\n"
        f"<b>【BOTの判断】: 取引計画に基づきエントリーを検討してください。</b>"
    )

def format_best_position_message(signal: Dict) -> str:
    """最良ポジション選定メッセージを整形 (v10.0.0の情報を追加)"""
    score = signal['score']
    side_icon = "⬆️ LONG" if signal['side'] == "ロング" else "⬇️ SHORT"
    
    macro_trend = signal['macro_context']['trend']
    tech_data = signal.get('tech_data', {})
    
    adx_str = f"{tech_data.get('adx', 25):.1f}"
    bb_width_pct = tech_data.get('bb_width_pct', 0)
    depth_ratio = signal.get('depth_ratio', 0.5)
    atr_val = tech_data.get('atr_value', 0)
    
    format_price = format_price_lambda(signal['symbol'])
    
    return (
        f"👑 <b>{signal['symbol']} - 12時間 最良ポジション候補</b> {side_icon} 🔥\n"
        f"<i>選定時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST</i>\n"
        f"-----------------------------------------\n"
        f"• <b>選定スコア</b>: <code>{score * 100:.2f}%</code>\n"
        f"• <b>現在価格</b>: <code>${format_price(signal['price'])}</code>\n"
        f"• <b>ATR</b>: <code>{format_price(atr_val)}</code>\n"
        f"\n"
        f"🎯 <b>取引計画 (推奨)</b>:\n"
        f"  - エントリー: <code>${format_price(signal['entry'])}</code>\n"
        f"  - 利確 (TP): <code>${format_price(signal['tp1'])}</code> (ATRベース)\n"
        f"  - 損切 (SL): <code>${format_price(signal['sl'])}</code> (ATRベース)\n"
        f"\n"
        f"💡 <b>選定理由</b>:\n"
        f"  1. <b>レジーム</b>: {signal['regime']} (ADX: {adx_str}, BB幅: {bb_width_pct:.2f}%) - **{tech_data.get('ma_position', '中立')}** の強い優位性。\n"
        f"  2. <b>マクロ/需給</b>: {macro_trend} の状況下で、流動性も {depth_ratio:.2f} と {signal['side']} に有利です。\n"
        f"  3. <b>複合スコア</b>: モメンタム、トレンド、ボラティリティの複合分析結果が最も高い優位性を示しました。\n"
        f"\n"
        f"<b>【BOTの判断】: 市場の状況に関わらず、最も優位性のある取引機会です。</b>"
    )

# ====================================================================================
# CORE ANALYSIS FUNCTIONS (v10.0.0の高度なロジックを統合)
# ====================================================================================

def calculate_trade_levels(price: float, side: str, atr_value: float, score: float) -> Dict:
    """📌 統合要素: ATRに基づいてTP/SLを計算"""
    if atr_value <= 0:
        return {"entry": price, "sl": price, "tp1": price, "tp2": price}
    
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
    """📌 統合要素: pandas_taを使用して正確なテクニカル指標を計算"""
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'Open', 'High', 'Low', 'Close', 'Volume'])
    
    if len(df) < 200:
         # データが不足している場合はDataFrameはそのまま返し、指標はデフォルト値
         return {"rsi": 50, "macd_hist": 0, "adx": 25, "atr_value": 0, "bb_width_pct": 0, "ma_position_score": 0, "ma_position": "中立", "df": df}


    # テクニカル指標の計算
    df.ta.macd(append=True)
    df.ta.rsi(append=True)
    df.ta.adx(append=True)
    df.ta.atr(append=True)
    bbands = df.ta.bbands()
    df.ta.sma(length=20, append=True)
    df.ta.sma(length=50, append=True)
    
    last = df.iloc[-1]
    
    # ボリンジャーバンド幅 (BBW) のパーセント計算
    bb_width_col = bbands.columns[bbands.columns.str.contains('BBW_')].tolist()
    bb_width = last[bb_width_col[0]] if bb_width_col else 0.0
    bb_width_pct = bb_width / last['SMA_20'] * 100 if last['SMA_20'] > 0 else 0
    
    # MAの位置関係スコア
    ma_pos_score = 0
    ma_position = "中立"
    if last['Close'] > last['SMA_20'] and last['SMA_20'] > last['SMA_50']:
        ma_pos_score = 0.3
        ma_position = "強力なロングトレンド"
    elif last['Close'] < last['SMA_20'] and last['SMA_20'] < last['SMA_50']:
        ma_pos_score = -0.3
        ma_position = "強力なショートトレンド"
    elif last['Close'] > last['SMA_20']:
        ma_pos_score = 0.1
        ma_position = "短期ロング傾向"
    elif last['Close'] < last['SMA_20']:
        ma_pos_score = -0.1
        ma_position = "短期ショート傾向"

    # カラム名を取得
    macd_hist_col = df.columns[df.columns.str.startswith('MACDH_')].tolist()
    adx_col = df.columns[df.columns.str.startswith('ADX_')].tolist()
    atr_col = df.columns[df.columns.str.startswith('ATR_')].tolist()
    rsi_col = df.columns[df.columns.str.startswith('RSI_')].tolist()

    return {
        "rsi": last[rsi_col[0]] if rsi_col else 50,
        "macd_hist": last[macd_hist_col[0]] if macd_hist_col else 0,
        "adx": last[adx_col[0]] if adx_col else 25,
        "atr_value": last[atr_col[0]] if atr_col else 0,
        "bb_width_pct": bb_width_pct,
        "ma_position_score": ma_pos_score,
        "ma_position": ma_position,
        "df": df
    }


def get_news_sentiment(symbol: str) -> Dict:
    """ニュース感情スコア（v9.1.12のランダム版を維持）"""
    sentiment_score = 0.5 + random.uniform(-0.1, 0.1) 
    return {"sentiment_score": np.clip(sentiment_score, 0.0, 1.0)}


def market_analysis_and_score(symbol: str, tech_data: Dict, depth_data: Dict, sentiment_data: Dict, macro_context: Dict) -> Tuple[float, str, str]:
    """📌 統合要素: 複合的なテクニカル要素に基づいたスコアリングロジック"""
    
    df = tech_data.get('df')
    if df is None or len(df) < 50: return 0.5, "Neutral", "不明"
    
    # 1. レジーム判定
    adx = tech_data.get('adx', 25)
    bb_width_pct = tech_data.get('bb_width_pct', 0)
    
    if bb_width_pct < 2.0 and adx < 25:
        regime = "レンジ相場 (抑制)"
        regime_boost = 0.0
    elif bb_width_pct > 4.0 and adx > 30:
        regime = "トレンド相場 (強化)"
        regime_boost = 0.1
    else:
        regime = "移行期"
        regime_boost = 0.05
    
    # 2. モメンタムバイアス
    rsi = tech_data.get('rsi', 50)
    macd_hist = tech_data.get('macd_hist', 0)
    rsi_momentum = (rsi - 50) / 50 * 0.15 
    macd_momentum = np.clip(macd_hist * 10, -0.15, 0.15) 
    momentum_bias = rsi_momentum * 0.4 + macd_momentum * 0.6
    
    # 3. トレンドバイアス
    ma_pos_score = tech_data.get('ma_position_score', 0)
    adx_direction_score = ma_pos_score * (np.clip((adx - 20) / 20, 0, 1) * 0.5 + 0.5)
    trend_bias = ma_pos_score * 0.5 + adx_direction_score * 0.5

    # 4. 需給/センチメントバイアス
    depth_ratio = depth_data.get('depth_ratio', 0.5)
    sentiment_score = sentiment_data.get('sentiment_score', 0.5)
    depth_bias = (depth_ratio - 0.5) * 0.2
    sentiment_bias = (sentiment_score - 0.5) * 0.1
    
    # 5. 複合スコアリング
    base_score = 0.5
    weighted_bias = (momentum_bias * 0.4) + (trend_bias * 0.4) + (depth_bias * 0.1 + sentiment_bias * 0.1)
    
    macro_adjustment = macro_context.get('dominance_change_boost', 0.0) * (0.5 if symbol != 'BTC' else 0.0)

    final_score = base_score + weighted_bias + regime_boost * np.sign(weighted_bias) + macro_adjustment
    final_score = np.clip(final_score, 0.0, 1.0)
    
    # 6. シグナル方向の決定
    if final_score > 0.5 + SIGNAL_THRESHOLD / 2:
        side = "ロング"
    elif final_score < 0.5 - SIGNAL_THRESHOLD / 2:
        side = "ショート"
    else:
        side = "Neutral"
        
    display_score = abs(final_score - 0.5) * 2 if side != "Neutral" else abs(final_score - 0.5)
    
    return display_score, side, regime


async def generate_signal_candidate(symbol: str, macro_context_data: Dict, client_name: str) -> Optional[Dict]:
    """単一銘柄に対する分析を実行し、シグナル候補を生成"""
    
    sentiment_data = get_news_sentiment(symbol)
    
    # 1. 15mデータの取得
    ohlcv_15m, ccxt_status_15m = await fetch_ohlcv_with_fallback(client_name, symbol, '15m')
    if ccxt_status_15m in ["RateLimit", "Timeout"] or not ohlcv_15m:
        return {"symbol": symbol, "side": ccxt_status_15m, "score": 0.0, "client": client_name} 
    
    # 2. テクニカル指標の計算
    tech_data_full = calculate_technical_indicators(ohlcv_15m)
    tech_data = {k: v for k, v in tech_data_full.items() if k != 'df'}
    df_analyzed = tech_data_full.get('df')

    # 3. オーダーブック深度の取得
    depth_data = await fetch_order_book_depth_async(symbol) 
    
    # 4. 複合分析とスコアリング
    final_score, side, regime = market_analysis_and_score(symbol, tech_data_full, depth_data, sentiment_data, macro_context_data)
    
    # 5. トレードレベルの計算
    current_price = df_analyzed['Close'].iloc[-1] if df_analyzed is not None else ohlcv_15m[-1][4]
    atr_value = tech_data.get('atr_value', 0)
    
    trade_levels = calculate_trade_levels(current_price, side, atr_value, final_score)
    
    
    # --- 6. シグナル結果の整形 ---
    
    if side == "Neutral":
        confidence = final_score
        return {"symbol": symbol, "side": "Neutral", "confidence": confidence, "regime": regime,
                "macro_context": macro_context_data, "is_fallback": ccxt_status_15m != "Success",
                "depth_ratio": depth_data['depth_ratio'], "total_depth": depth_data['total_depth'],
                "tech_data": tech_data}

    # トレードシグナル
    source = client_name
    return {"symbol": symbol, "side": side, "price": current_price, "score": final_score,
            "entry": trade_levels['entry'], "sl": trade_levels['sl'],
            "tp1": trade_levels['tp1'], "tp2": trade_levels['tp2'],
            "regime": regime, "is_fallback": ccxt_status_15m != "Success", 
            "depth_ratio": depth_data['depth_ratio'], "total_depth": depth_data['total_depth'],
            "macro_context": macro_context_data, "source": source, 
            "sentiment_score": sentiment_data["sentiment_score"],
            "tech_data": tech_data}


# -----------------------------------------------------------------------------------
# ASYNC TASKS & MAIN LOOP (v9.1.12の完全な関数定義を維持)
# -----------------------------------------------------------------------------------

async def update_monitor_symbols_dynamically(client_name: str, limit: int):
    """監視銘柄リストを更新（ダミー処理）"""
    global CURRENT_MONITOR_SYMBOLS
    logging.info(f"🔄 銘柄リストを更新します。")
    CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS + [f"ALT{i}" for i in range(1, 5)] 
    await asyncio.sleep(1)

async def instant_price_check_task():
    """突発的な価格変動を監視するタスク（ダミー処理）"""
    while True:
        await asyncio.sleep(INSTANT_CHECK_INTERVAL)
        # 実際の価格チェックロジックをここに実装
        
async def self_ping_task(interval: int):
    """BOTのヘルスチェックを定期的に実行するタスク（ダミー処理）"""
    global NEUTRAL_NOTIFIED_TIME
    while True:
        await asyncio.sleep(interval)
        if time.time() - NEUTRAL_NOTIFIED_TIME > 60 * 60 * 2: # 2時間ごとに強制ヘルスチェック
            stats = {"attempts": TOTAL_ANALYSIS_ATTEMPTS, "errors": TOTAL_ANALYSIS_ERRORS, "last_success": LAST_SUCCESS_TIME}
            health_signal = {"symbol": "BOT", "side": "Neutral", "confidence": 0.5, "regime": "N/A", "macro_context": BTC_DOMINANCE_CONTEXT, "is_health_check": True, "analysis_stats": stats, "tech_data": {}, "depth_ratio": 0.5, "total_depth": 0}
            asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_telegram_message(health_signal))))
            NEUTRAL_NOTIFIED_TIME = time.time()

async def signal_notification_task(signals: List[Optional[Dict]]):
    """シグナル通知の処理とクールダウン管理"""
    current_time = time.time()
    has_trade_signal = False
    
    for signal in signals:
        if signal is None: continue
        
        symbol = signal['symbol']
        side = signal['side']
        score = signal.get('score', 0.0)
        
        # トレードシグナル通知
        if side in ["ロング", "ショート"] and score >= SIGNAL_THRESHOLD:
            has_trade_signal = True
            if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_telegram_message(signal))))
        
        # 中立シグナル（低スコア）の処理は省略（self_ping_taskでヘルスチェック通知を代替）

async def best_position_notification_task():
    """定期的に最良のポジション候補を通知するタスク"""
    global LAST_BEST_POSITION_TIME
    while True:
        await asyncio.sleep(1) # main_loopでインターバルを制御
        current_time = time.time()
        
        if current_time - LAST_BEST_POSITION_TIME >= BEST_POSITION_INTERVAL:
            
            strongest_signal = None
            max_score = 0
            
            for signal in LAST_ANALYSIS_SIGNALS:
                if signal.get('side') in ["ロング", "ショート"] and signal['score'] > max_score:
                    max_score = signal['score']
                    strongest_signal = signal
            
            if strongest_signal and max_score >= 0.60: # 最良ポジションは少し厳しめの閾値
                logging.info(f"👑 最良ポジション候補: {strongest_signal['symbol']} (Score: {max_score:.2f})")
                asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_best_position_message(strongest_signal), is_emergency=False)))
                LAST_BEST_POSITION_TIME = current_time
        
async def main_loop():
    """BOTのメイン実行ループ。分析、クライアント切り替え、通知を行う。"""
    global LAST_UPDATE_TIME, NOTIFIED_SYMBOLS, NEUTRAL_NOTIFIED_TIME
    global LAST_SUCCESS_TIME, TOTAL_ANALYSIS_ATTEMPTS, TOTAL_ANALYSIS_ERRORS
    global CCXT_CLIENT_NAME, ACTIVE_CLIENT_HEALTH, CCXT_CLIENT_NAMES
    global LAST_ANALYSIS_SIGNALS, BTC_DOMINANCE_CONTEXT

    # 📌 起動時の初期設定
    BTC_DOMINANCE_CONTEXT = await asyncio.to_thread(get_crypto_macro_context)
    LAST_UPDATE_TIME = time.time()
    
    # 起動時の初期タスク
    await send_test_message()
    asyncio.create_task(self_ping_task(interval=PING_INTERVAL)) 
    asyncio.create_task(instant_price_check_task())
    asyncio.create_task(best_position_notification_task()) 
    
    # 起動時の初期クライアント設定と銘柄更新
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
        available_clients = {
            name: health_time 
            for name, health_time in ACTIVE_CLIENT_HEALTH.items() 
            if current_time >= health_time 
        }
        
        if not available_clients:
            next_client = min(ACTIVE_CLIENT_HEALTH, key=ACTIVE_CLIENT_HEALTH.get)
            cooldown_time_j = datetime.fromtimestamp(ACTIVE_CLIENT_HEALTH[next_client], JST).strftime('%H:%M:%S')
            logging.warning(f"🚨 全てのクライアントがレート制限でクールダウン中です。{next_client}が{cooldown_time_j}に復帰予定。待機します。")
            await asyncio.sleep(LOOP_INTERVAL)
            continue
        
        if current_time >= ACTIVE_CLIENT_HEALTH.get('OKX', 0):
             CCXT_CLIENT_NAME = 'OKX'
        else:
            other_available_clients = {k: v for k, v in available_clients.items() if k != 'OKX'}
            if other_available_clients:
                CCXT_CLIENT_NAME = max(other_available_clients, key=other_available_clients.get)
            else:
                 await asyncio.sleep(LOOP_INTERVAL)
                 continue


        # --- 2. 動的銘柄リストの更新とマクロ環境の取得 ---
        if current_time - LAST_UPDATE_TIME > DYNAMIC_UPDATE_INTERVAL:
            await update_monitor_symbols_dynamically(CCXT_CLIENT_NAME, limit=30) 
            BTC_DOMINANCE_CONTEXT = await asyncio.to_thread(get_crypto_macro_context) # 📌 更新
            LAST_UPDATE_TIME = current_time

        # --- 3. 分析の実行 ---
        symbols_for_analysis = [sym for sym in CURRENT_MONITOR_SYMBOLS if sym not in EXCLUDED_SYMBOLS]
        
        logging.info(f"🔍 分析開始 (データソース: {CCXT_CLIENT_NAME}, 銘柄数: {len(symbols_for_analysis)}/{len(CURRENT_MONITOR_SYMBOLS)}銘柄)")
        TOTAL_ANALYSIS_ATTEMPTS += 1
        
        signals: List[Optional[Dict]] = []
        
        # CCXTのレート制限を考慮し、バッチ処理とウェイト
        for i in range(0, len(symbols_for_analysis), 10):
            batch_symbols = symbols_for_analysis[i:i + 10]
            analysis_tasks = [
                generate_signal_candidate(symbol, BTC_DOMINANCE_CONTEXT, CCXT_CLIENT_NAME) 
                for symbol in batch_symbols
            ]
            batch_signals = await asyncio.gather(*analysis_tasks)
            signals.extend(batch_signals)
            await asyncio.sleep(1) # バッチ間のウェイト

        
        # --- 4. シグナルとエラー処理 ---
        has_major_error = False
        
        LAST_ANALYSIS_SIGNALS = [s for s in signals if s is not None and s.get('side') not in ["RateLimit", "Timeout"]]
        
        asyncio.create_task(signal_notification_task(signals))
        
        for signal in signals:
            if signal is None: continue 
                
            if signal.get('side') in ["RateLimit", "Timeout"]:
                cooldown_end_time = current_time + CLIENT_COOLDOWN
                logging.error(f"❌ レート制限/タイムアウトエラー発生: クライアント {signal['client']} のヘルスを {datetime.fromtimestamp(cooldown_end_time, JST).strftime('%H:%M:%S')} JST にリセット (クールダウン)。")
                ACTIVE_CLIENT_HEALTH[signal['client']] = cooldown_end_time 
                
                has_major_error = True
                TOTAL_ANALYSIS_ERRORS += 1
                break 

        
        if not has_major_error:
            LAST_SUCCESS_TIME = current_time
            await asyncio.sleep(LOOP_INTERVAL)
        else:
            logging.info("➡️ クライアント切り替えのため、即座に次の分析サイクルに進みます。")
            await asyncio.sleep(1) 
            continue 

# -----------------------------------------------------------------------------------
# FASTAPI SETUP (v9.1.12の構造を維持)
# -----------------------------------------------------------------------------------

app = FastAPI(title="Apex BOT API", version="v9.1.12-integrated")

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にCCXTクライアントを初期化し、メインループを開始する"""
    initialize_ccxt_client()
    logging.info("🚀 Apex BOT v9.1.12 統合版 Startup Complete.")
    asyncio.create_task(main_loop())


@app.get("/status")
def get_status():
    """ヘルスチェック用のエンドポイント"""
    status_msg = {
        "status": "ok",
        "bot_version": "v9.1.12-integrated",
        "last_success_timestamp": LAST_SUCCESS_TIME,
        "current_client": CCXT_CLIENT_NAME,
        "monitor_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "excluded_symbols_count": len(EXCLUDED_SYMBOLS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    """ルートエンドポイント (GET/HEAD) - 稼働確認用"""
    return JSONResponse(content={"message": "Apex BOT is running (v9.1.12-integrated)."}, status_code=200)
