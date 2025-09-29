# ====================================================================================
# Apex BOT v9.0.0 - 流動性・環境優位性 強化版
# ====================================================================================

# 1. 必要なライブラリをインポート
import os
import time
import logging
import requests
import ccxt.async_support as ccxt_async
import numpy as np
import pandas as pd
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple, Any
import yfinance as yf
import asyncio
import random
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse 
import uvicorn
from dotenv import load_dotenv

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 📌 初期監視対象銘柄リスト 
DEFAULT_SYMBOLS = ["BTC", "ETH", "SOL", "XRP", "ADA", "DOGE"]

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# 📌 v9.0.0 設定
LOOP_INTERVAL = 60      # メイン分析ループ間隔を60秒に維持
PING_INTERVAL = 8       # 自己Ping間隔を8秒に維持 (Renderのアイドルタイムアウト回避)
PING_TIMEOUT = 12       # Pingのタイムアウト時間を12秒に維持
DYNAMIC_UPDATE_INTERVAL = 600 # マクロ分析/銘柄更新間隔 (10分)
REQUEST_DELAY = 0.5     # CCXTリクエスト間の遅延 (0.5秒)
MIN_SLEEP_AFTER_IO = 0.005 # IO解放のための最小スリープ時間

# 突発変動検知設定 (v8.9.5から維持)
INSTANT_CHECK_INTERVAL = 15 # 即時価格チェック間隔（秒）
MAX_PRICE_DEVIATION_PCT = 1.5 # 15分間の価格変動率の閾値（%）
INSTANT_CHECK_WINDOW_MIN = 15 # 変動をチェックする期間（分）
ORDER_BOOK_DEPTH_LEVELS = 10 # 📌 オーダーブック分析の深度を10層に増加 (v9.0.0で強化)

# ログ設定
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    force=True)

# CCXTのログをWarningレベルに抑制
logging.getLogger('ccxt').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# グローバル状態変数
CCXT_CLIENTS_DICT: Dict[str, ccxt_async.Exchange] = {}
CCXT_CLIENT_NAMES: List[str] = []
CCXT_CLIENT_NAME: str = 'Initializing' 
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS
NOTIFIED_SYMBOLS: Dict[str, float] = {} 
NEUTRAL_NOTIFIED_TIME: float = 0
LAST_SUCCESS_TIME: float = 0.0
TOTAL_ANALYSIS_ATTEMPTS: int = 0
TOTAL_ANALYSIS_ERRORS: int = 0
ACTIVE_CLIENT_HEALTH: Dict[str, float] = {} 
PRICE_HISTORY: Dict[str, List[Tuple[float, float]]] = {} 


# ====================================================================================
# UTILITIES & CLIENTS (変更なし、一部共通ユーティリティを流用)
# ====================================================================================

def initialize_ccxt_client():
    """CCXTクライアントを初期化（複数クライアントで負荷分散）"""
    global CCXT_CLIENTS_DICT, CCXT_CLIENT_NAMES, ACTIVE_CLIENT_HEALTH

    clients = {
        'Binance': ccxt_async.binance({"enableRateLimit": True, "timeout": 20000}),
        'Bybit': ccxt_async.bybit({"enableRateLimit": True, "timeout": 30000}), 
        'OKX': ccxt_async.okx({"enableRateLimit": True, "timeout": 30000}),     
        'Coinbase': ccxt_async.coinbase({"enableRateLimit": True, "timeout": 20000,
                                         "options": {"defaultType": "spot", "fetchTicker": "public"}}),
    }

    CCXT_CLIENTS_DICT = clients
    CCXT_CLIENT_NAMES = list(CCXT_CLIENTS_DICT.keys())
    ACTIVE_CLIENT_HEALTH = {name: time.time() for name in CCXT_CLIENT_NAMES}


async def send_test_message():
    """起動テスト通知 (v9.0.0に更新)"""
    test_text = (
        f"🤖 <b>Apex BOT v9.0.0 - 起動テスト通知</b> 🚀\n\n"
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"<b>機能強化: 流動性分析・市場環境適応ロジックを大幅に強化しました。</b>"
    )
    try:
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, lambda: send_telegram_html(test_text, is_emergency=True))
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")

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


# ====================================================================================
# CORE ANALYSIS FUNCTIONS (v9.0.0 強化)
# ====================================================================================

def get_tradfi_macro_context() -> Dict:
    """伝統的金融市場（VIXなど）からマクロ環境のコンテクストを取得（ブロッキング関数）"""
    context = {"trend": "中立", "vix_level": 0.0, "gvix_level": 0.0, "risk_appetite_boost": 0.0}
    try:
        vix = yf.Ticker("^VIX").history(period="1d", interval="1h")
        if not vix.empty:
            vix_level = vix['Close'].iloc[-1]
            context["vix_level"] = vix_level
            
            # 📌 VIXに基づくリスクセンチメントの調整 (v9.0.0強化)
            if vix_level < 15:
                context["trend"] = "リスクオン (VIX低)"
                context["risk_appetite_boost"] = 0.05 # リスクオン環境でロングにボーナス
            elif vix_level > 25:
                context["trend"] = "リスクオフ (VIX高)"
                context["risk_appetite_boost"] = -0.05 # リスクオフ環境でショートにボーナス
            else:
                context["trend"] = "中立"

        context["gvix_level"] = random.uniform(40, 60) # 簡易的な仮想通貨ボラティリティ指標
    except Exception:
        pass
    return context

def calculate_trade_levels(closes: pd.Series, side: str, score: float, adx_level: float) -> Dict:
    """価格データと信頼度スコア、ADXからエントリー/決済レベルを計算（v9.0.0 強化）"""
    if len(closes) < 20:
        current_price = closes.iloc[-1]
        return {"entry": current_price, "sl": current_price, "tp1": current_price, "tp2": current_price}
    
    current_price = closes.iloc[-1]
    volatility_range = closes.diff().abs().std() * 2 
    
    # 📌 ADXに基づくTP/SL幅の動的調整 (v9.0.0 強化)
    # ADXが高い（トレンド）ほどTP幅を広げ、SLをタイトに（トレンドフォロー）
    # ADXが低い（レンジ）ほどTP幅を狭く、SLを広く（レンジ逆張り）
    adx_factor = np.clip((adx_level - 20) / 20, 0.5, 1.5) # ADX 20で1.0、40で1.5、10で0.5
    
    # スコアとADXを統合した最終調整係数
    multiplier = (1.0 + score * 0.5) * adx_factor 
    
    if side == "ロング":
        entry = current_price * 0.9995 
        sl = current_price - (volatility_range * (2.0 - adx_factor)) # レンジではSLを広く
        tp1 = current_price + (volatility_range * 1.5 * multiplier) 
        tp2 = current_price + (volatility_range * 3.0 * multiplier)
    else:
        entry = current_price * 1.0005 
        sl = current_price + (volatility_range * (2.0 - adx_factor))
        tp1 = current_price - (volatility_range * 1.5 * multiplier)
        tp2 = current_price - (volatility_range * 3.0 * multiplier)
        
    return {"entry": entry, "sl": sl, "tp1": tp1, "tp2": tp2}


# -----------------------------------------------------------------------------------
# CCXT WRAPPER FUNCTIONS (v9.0.0 強化)
# -----------------------------------------------------------------------------------

async def fetch_order_book_depth_async(symbol: str) -> Dict:
    """オーダーブックの深度を取得し、買い/売り圧の比率を計算（v9.0.0 強化）"""
    client = CCXT_CLIENTS_DICT.get(CCXT_CLIENT_NAME)
    if client is None: return {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5, "total_depth": 0}

    market_symbol = f"{symbol}/USDT" 

    try:
        await asyncio.sleep(REQUEST_DELAY)
        # 深度をORDER_BOOK_DEPTH_LEVELSで定義した層数（10層）まで取得
        order_book = await client.fetch_order_book(market_symbol, limit=20) 
        await asyncio.sleep(MIN_SLEEP_AFTER_IO) 
        
        # 📌 上位10層のボリュームを加重平均（v9.0.0 強化）
        # 価格に近い層ほど重み付けを大きくする (価格*量の合計)
        bid_volume_weighted = sum(amount * price for price, amount in order_book['bids'][:ORDER_BOOK_DEPTH_LEVELS])
        ask_volume_weighted = sum(amount * price for price, amount in order_book['asks'][:ORDER_BOOK_DEPTH_LEVELS])

        total_volume = bid_volume_weighted + ask_volume_weighted
        depth_ratio = bid_volume_weighted / total_volume if total_volume > 0 else 0.5

        return {"bid_volume": bid_volume_weighted, "ask_volume": ask_volume_weighted, 
                "depth_ratio": depth_ratio, "total_depth": total_volume}

    except Exception:
        return {"bid_volume": 0, "ask_volume": 0, "depth_ratio": 0.5, "total_depth": 0}

async def generate_signal_candidate(symbol: str, macro_context_data: Dict, client_name: str) -> Optional[Dict]:
    """単一銘柄に対する分析を実行し、シグナル候補を生成（v9.0.0 強化）"""
    
    sentiment_data = get_news_sentiment(symbol)
    ohlcv_15m, ccxt_status = await fetch_ohlcv_single_client(client_name, symbol, '15m', 100)

    if ccxt_status in ["RateLimit", "Timeout"]:
        return {"symbol": symbol, "side": ccxt_status, "score": 0.0, "client": client_name}
    if ccxt_status != "Success":
        return None

    win_prob, tech_data = get_ml_prediction(ohlcv_15m, sentiment_data)
    closes = pd.Series([c[4] for c in ohlcv_15m])
    
    wave_score, wave_phase = calculate_elliott_wave_score(closes)
    depth_data = await fetch_order_book_depth_async(symbol) 
    source = client_name
    adx_level = tech_data.get('adx', 25)

    # --- 中立シグナル判定 ---
    if 0.47 < win_prob < 0.53:
        confidence = abs(win_prob - 0.5)
        regime = "レンジ相場" if adx_level < 25 else "移行期"
        return {"symbol": symbol, "side": "Neutral", "confidence": confidence, "regime": regime,
                "macro_context": macro_context_data, "is_fallback": False,
                "wave_phase": wave_phase, "depth_ratio": depth_data['depth_ratio'],
                "total_depth": depth_data['total_depth'],
                "tech_data": tech_data, "sentiment_score": sentiment_data["sentiment_score"]}

    # --- トレードシグナル判定とスコアリング (v9.0.0 強化) ---
    side = "ロング" if win_prob >= 0.53 else "ショート"
    
    # 1. 基本スコア
    base_score = abs(win_prob - 0.5) * 2 
    base_score *= (0.8 + wave_score * 0.4) 

    # 2. ADX（トレンド強度）調整
    if adx_level > 30: 
        adx_boost = 0.1 # トレンドに乗る
        regime = "トレンド相場"
    elif adx_level < 20: 
        adx_boost = -0.05 # レンジならスコア減点 (予測の難易度上昇)
        regime = "レンジ相場"
    else: 
        adx_boost = 0.0
        regime = "移行期"

    # 3. センチメント調整 (ニュース感情)
    sentiment_boost = (sentiment_data["sentiment_score"] - 0.5) * 0.2
    if side == "ショート": sentiment_boost *= -1 

    # 4. 深度（板の厚み）調整 (v9.0.0 強化: 調整幅を拡大)
    depth_adjustment = 0.0
    if side == "ロング":
        # 買い圧が高ければ（0.5超）ブースト、低ければペナルティ
        depth_adjustment = (depth_data['depth_ratio'] - 0.5) * 0.4 
    else:
        # 売り圧が高ければ（0.5未満）ブースト
        depth_adjustment = (0.5 - depth_data['depth_ratio']) * 0.4 

    # 5. マクロ環境調整 (v9.0.0 強化)
    macro_boost = macro_context_data.get('risk_appetite_boost', 0.0)
    if side == "ショート": macro_boost *= -1 # リスクオンではショートにペナルティ

    # 6. 最終スコア算出
    final_score = np.clip((base_score + depth_adjustment + adx_boost + sentiment_boost + macro_boost), 0.0, 1.0)

    # 7. トレードレベル計算 (ADX情報も渡す)
    trade_levels = calculate_trade_levels(closes, side, final_score, adx_level)

    return {"symbol": symbol, "side": side, "price": closes.iloc[-1], "score": final_score,
            "entry": trade_levels['entry'], "sl": trade_levels['sl'],
            "tp1": trade_levels['tp1'], "tp2": trade_levels['tp2'],
            "regime": regime, "is_fallback": False,
            "wave_phase": wave_phase, "depth_ratio": depth_data['depth_ratio'],
            "total_depth": depth_data['total_depth'],
            "vix_level": macro_context_data['vix_level'], "macro_context": macro_context_data,
            "source": source, "sentiment_score": sentiment_data["sentiment_score"],
            "tech_data": tech_data}


# -----------------------------------------------------------------------------------
# TELEGRAM FORMATTING (v9.0.0 強化)
# -----------------------------------------------------------------------------------

def format_telegram_message(signal: Dict) -> str:
    """シグナルデータからTelegram通知メッセージを整形（v9.0.0 強化）"""
    
    # 共通情報の取得
    vix_level = signal['macro_context']['vix_level']
    vix_status = f"VIX: {vix_level:.1f}" if vix_level > 0 else "VIX: N/A"
    macro_trend = signal['macro_context']['trend']
    tech_data = signal.get('tech_data', {})
    adx_str = f"{tech_data.get('adx', 25):.1f}"
    depth_ratio = signal.get('depth_ratio', 0.5)
    total_depth = signal.get('total_depth', 0)
    
    # 流動性ステータス
    if total_depth > 10000000: # 例: $10M以上
        liquidity_status = f"非常に厚い (${total_depth/1e6:.1f}M)"
    elif total_depth > 1000000: # 例: $1M以上
        liquidity_status = f"厚い (${total_depth/1e6:.1f}M)"
    else:
        liquidity_status = f"普通〜薄い (${total_depth/1e6:.1f}M)"
    
    depth_status = "買い圧優勢" if depth_ratio > 0.52 else ("売り圧優勢" if depth_ratio < 0.48 else "均衡")
    format_price = lambda p: format_price_utility(p, signal['symbol'])
    
    # --- 1. 中立/ヘルス通知 ---
    if signal['side'] == "Neutral":
        
        # システムヘルス通知
        if signal.get('is_fallback', False):
            # (ヘルス通知のロジックはv8.9.5から変更なし)
            stats = signal.get('analysis_stats', {"attempts": 0, "errors": 0, "last_success": 0})
            error_rate = (stats['errors'] / stats['attempts']) * 100 if stats['attempts'] > 0 else 0
            last_success_time = datetime.fromtimestamp(stats['last_success'], JST).strftime('%H:%M:%S') if stats['last_success'] > 0 else "N/A"
            return (
                f"🚨 <b>Apex BOT v9.0.0 - 死活監視 (システム正常)</b> 🟢\n"
                f"<i>強制通知時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST</i>\n\n"
                f"• **市場コンテクスト**: {macro_trend} ({vix_status})\n"
                f"• **🤖 BOTヘルス**: 最終成功: {last_success_time} JST (エラー率: {error_rate:.1f}%)\n"
                f"<b>【BOTの判断】: データ取得に問題はありませんが、待機中です。</b>"
            )

        # 通常の中立分析結果 (v9.0.0 強化)
        rsi_str = f"{tech_data.get('rsi', 50):.1f}"
        macd_hist_str = f"{tech_data.get('macd_hist', 0):.4f}"
        cci_str = f"{tech_data.get('cci_signal', 0):.1f}"
        sentiment_pct = signal.get('sentiment_score', 0.5) * 100
        confidence_pct = signal['confidence'] * 200

        return (
            f"⚠️ <b>{signal['symbol']} - 市場分析速報 (中立)</b> ⏸️\n"
            f"<b>信頼度: {confidence_pct:.1f}%</b>\n"
            f"---------------------------\n"
            f"• <b>市場環境/レジーム</b>: {signal['regime']} (ADX: {adx_str}) | {macro_trend} ({vix_status})\n"
            f"• <b>流動性/需給</b>: {liquidity_status} | {depth_status} (比率: {depth_ratio:.2f})\n"
            f"• <b>波形</b>: {signal['wave_phase']}\n"
            f"\n"
            f"📊 <b>テクニカル詳細</b>:\n"
            f"  - <i>RSI/CCI</i>: {rsi_str} / {cci_str} | <i>MACD Hist</i>: {macd_hist_str}\n" 
            f"  - <i>ニュース感情</i>: {sentiment_pct:.1f}% Positive\n"
            f"\n"
            f"<b>【BOTの判断】: 現在はボラティリティが低く、方向性が不鮮明です。様子見推奨。</b>"
        )

    # --- 2. トレードシグナル通知 ---
    score = signal['score']
    side_icon = "⬆️ LONG" if signal['side'] == "ロング" else "⬇️ SHORT"
    source = signal.get('source', 'N/A')

    # スコアに基づく推奨ロットとアクション
    if score >= 0.80: score_icon = "🔥🔥🔥"; lot_size = "MAX"; action = "積極的なエントリー"
    elif score >= 0.65: score_icon = "🔥🌟"; lot_size = "中〜大"; action = "標準的なエントリー"
    elif score >= 0.55: score_icon = "✨"; lot_size = "小"; action = "慎重なエントリー"
    else: score_icon = "🔹"; lot_size = "最小限"; action = "見送りまたは極めて慎重に"

    rsi_str = f"{tech_data.get('rsi', 50):.1f}"
    macd_hist_str = f"{tech_data.get('macd_hist', 0):.4f}"
    cci_str = f"{tech_data.get('cci_signal', 0):.1f}"
    sentiment_pct = signal.get('sentiment_score', 0.5) * 100

    return (
        f"{score_icon} <b>{signal['symbol']} - {side_icon} シグナル発生!</b> {score_icon}\n"
        f"<b>信頼度スコア: {score * 100:.2f}%</b>\n"
        f"-----------------------------------------\n"
        f"• <b>現在価格</b>: <code>${format_price(signal['price'])}</code>\n"
        f"\n"
        f"🎯 <b>エントリー (Entry)</b>: **<code>${format_price(signal['entry'])}</code>**\n"
        f"🟢 <b>利確 (TP1)</b>: **<code>${format_price(signal['tp1'])}</code>**\n"
        f"🔴 <b>損切 (SL)</b>: **<code>${format_price(signal['sl'])}</code>**\n"
        f"\n"
        f"📈 <b>複合分析詳細</b>:\n"
        f"  - <i>市場レジーム</i>: {signal['regime']} (ADX: {adx_str}) | <i>波形</i>: {signal['wave_phase']}\n"
        f"  - <i>流動性/需給</i>: {liquidity_status} | {depth_status} (比率: {depth_ratio:.2f})\n"
        f"  - <i>モメンタム/過熱</i>: RSI: {rsi_str} | MACD Hist: {macd_hist_str} | CCI: {cci_str}\n"
        f"  - <i>マクロ環境</i>: {macro_trend} ({vix_status}) | 感情: {sentiment_pct:.1f}% Positive (ソース: {source})\n"
        f"\n"
        f"💰 <b>取引示唆</b>:\n"
        f"  - <b>推奨ロット</b>: {lot_size}\n"
        f"  - <b>推奨アクション</b>: {action}\n"
        f"<b>【BOTの判断】: 取引計画に基づきエントリーを検討してください。</b>"
    )

# --- (他のタスク関数はv8.9.5から変更なし) ---

async def fetch_ohlcv_single_client(client_name: str, symbol: str, timeframe: str, limit: int) -> Tuple[List[list], str]:
    """単一のCCXTクライアントからOHLCVデータを取得し、I/O解放スリープを挿入"""
    client = CCXT_CLIENTS_DICT.get(client_name)
    if client is None: return [], "NoClient"
    trial_symbols = [f"{symbol}/USDT"]
    if client_name == 'Coinbase': trial_symbols.insert(0, f"{symbol}-USD")
    elif client_name == 'Binance': trial_symbols.append(f"{symbol}/BUSD")
    for market_symbol in trial_symbols:
        try:
            await asyncio.sleep(REQUEST_DELAY) 
            ohlcv = await client.fetch_ohlcv(market_symbol, timeframe, limit=limit)
            await asyncio.sleep(MIN_SLEEP_AFTER_IO) 
            if ohlcv and len(ohlcv) >= limit:
                global ACTIVE_CLIENT_HEALTH
                ACTIVE_CLIENT_HEALTH[client_name] = time.time() 
                return ohlcv, "Success"
        except ccxt_async.RateLimitExceeded:
            logging.warning(f"⚠️ CCXT ({client_name}, {market_symbol}) RateLimitExceeded。即時クライアント切り替え要求。")
            return [], "RateLimit" 
        except ccxt_async.RequestTimeout:
            logging.warning(f"⚠️ CCXT ({client_name}, {market_symbol}) RequestTimeout。即時クライアント切り替え要求。")
            return [], "Timeout" 
        except Exception:
            continue
    return [], "NoData"

# ... (中略：get_news_sentiment, calculate_elliott_wave_score, calculate_technical_indicators, get_ml_prediction はv8.9.5から変更なし) ...
def get_news_sentiment(symbol: str) -> Dict:
    """ニュース感情スコアを取得（実際はAPI利用だが、ここではダミー）"""
    sentiment_score = 0.5
    if random.random() < 0.1: sentiment_score = 0.65 
    elif random.random() > 0.9: sentiment_score = 0.35 
    return {"sentiment_score": sentiment_score}

def calculate_elliott_wave_score(closes: pd.Series) -> Tuple[float, str]:
    """エリオット波動理論に基づいた波形フェーズとスコアを計算（簡易版）"""
    if len(closes) < 50: return 0.0, "不明"
    volatility = closes.pct_change().std()
    recent_trend_strength = closes.iloc[-1] / closes.iloc[-20:].mean() - 1
    if volatility < 0.005 and abs(recent_trend_strength) < 0.01:
        wave_score = 0.2
        wave_phase = "修正波 (レンジ)"
    elif abs(recent_trend_strength) > 0.05 and volatility > 0.01:
        wave_score = 0.8
        wave_phase = "推進波 (トレンド)"
    else:
        wave_score = random.uniform(0.3, 0.7)
        wave_phase = "移行期"
    return wave_score, wave_phase

def calculate_technical_indicators(ohlcv: List[list]) -> Dict:
    """RSI, MACD, ADX, CCIを計算し、テクニカルデータを返す"""
    closes = pd.Series([c[4] for c in ohlcv])
    highs = pd.Series([c[2] for c in ohlcv])
    lows = pd.Series([c[3] for c in ohlcv])
    if len(closes) < 50:
        return {"rsi": 50, "macd_hist": 0, "macd_direction_boost": 0, "adx": 25, "cci_signal": 0}

    # 1. RSI (14)
    delta = closes.diff()
    gain = (delta.where(delta > 0, 0)).fillna(0)
    loss = (-delta.where(delta < 0, 0)).fillna(0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs)).iloc[-1]

    # 2. MACD (12, 26, 9)
    exp1 = closes.ewm(span=12, adjust=False).mean()
    exp2 = closes.ewm(span=26, adjust=False).mean()
    macd = exp1 - exp2
    signal_line = macd.ewm(span=9, adjust=False).mean()
    macd_hist = macd - signal_line
    macd_direction_boost = 0.0
    if macd_hist.iloc[-1] > 0 and macd_hist.iloc[-2] <= 0: macd_direction_boost = 0.05 
    elif macd_hist.iloc[-1] < 0 and macd_hist.iloc[-2] >= 0: macd_direction_boost = -0.05
    elif macd_hist.iloc[-1] > 0: macd_direction_boost = 0.02
    elif macd_hist.iloc[-1] < 0: macd_direction_boost = -0.02
    
    # 3. ADX (トレンド強度 - 簡易版)
    adx = ((highs - lows).abs() / closes).rolling(window=14).mean().iloc[-1] * 1000
    adx = np.clip(adx, 15, 60) 
    
    # 4. CCI (商品チャネル指数 - 過熱感)
    tp = (highs + lows + closes) / 3
    ma = tp.rolling(window=20).mean()
    md = tp.rolling(window=20).apply(lambda x: abs(x - x.mean()).mean(), raw=True) 
    cci = (tp - ma) / (0.015 * md)

    return {
        "rsi": rsi,
        "macd_hist": macd_hist.iloc[-1],
        "macd_direction_boost": macd_direction_boost,
        "adx": adx,
        "cci_signal": cci.iloc[-1]
    }

def get_ml_prediction(ohlcv: List[list], sentiment: Dict) -> Tuple[float, Dict]:
    """機械学習モデルの簡易シミュレーションとテクニカルデータの統合"""
    try:
        tech_data = calculate_technical_indicators(ohlcv)
        rsi = tech_data["rsi"]
        macd_boost = tech_data["macd_direction_boost"]
        cci = tech_data["cci_signal"]

        news_sentiment = sentiment.get("sentiment_score", 0.5)
        
        base_prob = 0.5 + ((rsi - 50) / 100) * 0.5 
        cci_boost = np.clip(cci / 500, -0.05, 0.05) * -1 

        final_prob = base_prob + macd_boost + (news_sentiment - 0.5) * 0.1 + cci_boost
        
        win_prob = np.clip(final_prob, 0.35, 0.65)
        
        return win_prob, tech_data

    except Exception:
        return 0.5, {"rsi": 50, "macd_hist": 0, "macd_direction_boost": 0, "adx": 25, "cci_signal": 0}

# ... (中略：update_monitor_symbols_dynamically, instant_price_check_task, self_ping_task はv8.9.5から変更なし) ...
async def update_monitor_symbols_dynamically(client_name: str, limit: int = 30) -> None:
    """取引量の多い上位銘柄を動的に取得し、監視リストを更新"""
    global CURRENT_MONITOR_SYMBOLS
    client = CCXT_CLIENTS_DICT.get(client_name)
    if client is None: return
    try:
        markets = await client.load_markets()
        await asyncio.sleep(MIN_SLEEP_AFTER_IO) 
        usdt_pairs = {
            symbol: market_data for symbol, market_data in markets.items()
            if 'USDT' in symbol and market_data.get('active', True)
        }
        target_symbols = list(usdt_pairs.keys())
        if len(target_symbols) > 150: 
            target_symbols = random.sample(target_symbols, 150) 
        tickers = await client.fetch_tickers(target_symbols)
        await asyncio.sleep(MIN_SLEEP_AFTER_IO) 
        valid_tickers = [
            t for t in tickers.values() if 
            t.get('quoteVolume') is not None and 
            t.get('quoteVolume', 0) > 0 and
            t.get('symbol') is not None and 
            'USDT' in t['symbol']
        ]
        sorted_tickers = sorted(
            valid_tickers,
            key=lambda x: x['quoteVolume'],
            reverse=True
        )
        new_symbols = [t['symbol'].split('/')[0].replace('-USD', '') for t in sorted_tickers][:limit]
        if len(new_symbols) > 5:
            CURRENT_MONITOR_SYMBOLS = list(set(new_symbols))
            logging.info(f"✅ 動的銘柄選定成功。{client_name}のTOP{len(new_symbols)}銘柄を監視対象に設定。")
        else:
            logging.warning(f"⚠️ 動的銘柄選定に失敗 (銘柄数不足: {len(new_symbols)}), デフォルトリストを維持。")
    except Exception as e:
        logging.error(f"❌ 動的銘柄選定エラー: {type(e).__name__}: {e}。既存リスト({len(CURRENT_MONITOR_SYMBOLS)}銘柄)を維持。")

async def fetch_current_price_single(client_name: str, symbol: str) -> Optional[float]:
    """単一のクライアントから現在の価格を取得"""
    client = CCXT_CLIENTS_DICT.get(client_name)
    market_symbol = f"{symbol}/USDT"
    if client_name == 'Coinbase': market_symbol = f"{symbol}-USD"
    if client is None: return None
    try:
        ticker = await client.fetch_ticker(market_symbol)
        await asyncio.sleep(MIN_SLEEP_AFTER_IO)
        return ticker['last']
    except Exception:
        return None

async def instant_price_check_task():
    """15秒ごとに監視銘柄の価格を取得し、突発的な変動をチェックする。"""
    global PRICE_HISTORY, CCXT_CLIENT_NAME
    INSTANT_NOTIFICATION_COOLDOWN = 180 # 3分間
    logging.info(f"⚡ 突発変動即時チェックタスクを開始します (インターバル: {INSTANT_CHECK_INTERVAL}秒)。")
    loop = asyncio.get_event_loop()
    while True:
        await asyncio.sleep(INSTANT_CHECK_INTERVAL)
        current_time = time.time()
        symbols_to_check = list(CURRENT_MONITOR_SYMBOLS)
        price_tasks = [fetch_current_price_single(CCXT_CLIENT_NAME, sym) for sym in symbols_to_check]
        prices = await asyncio.gather(*price_tasks)
        for symbol, price in zip(symbols_to_check, prices):
            if price is None:
                continue
            if symbol not in PRICE_HISTORY:
                PRICE_HISTORY[symbol] = []
            PRICE_HISTORY[symbol].append((current_time, price))
            cutoff_time = current_time - INSTANT_CHECK_WINDOW_MIN * 60
            PRICE_HISTORY[symbol] = [(t, p) for t, p in PRICE_HISTORY[symbol] if t >= cutoff_time]
            history = PRICE_HISTORY[symbol]
            if len(history) < 2:
                continue
            oldest_price = history[0][1]
            latest_price = price
            if oldest_price == 0: continue
            change_pct = (latest_price - oldest_price) / oldest_price * 100
            if abs(change_pct) >= MAX_PRICE_DEVIATION_PCT:
                is_not_recently_notified = current_time - NOTIFIED_SYMBOLS.get(symbol, 0) > INSTANT_NOTIFICATION_COOLDOWN
                if is_not_recently_notified:
                    side = "急騰" if change_pct > 0 else "急落"
                    message = format_instant_message(
                        symbol, side, change_pct, INSTANT_CHECK_WINDOW_MIN, latest_price, oldest_price
                    )
                    await loop.run_in_executor(None, lambda: send_telegram_html(message, is_emergency=True))
                    NOTIFIED_SYMBOLS[symbol] = current_time 
                    logging.warning(f"🚨 突発変動検知: {symbol} が {INSTANT_CHECK_WINDOW_MIN}分間で {change_pct:.2f}% {side}しました。即時通知実行。")

def blocking_ping(ping_url: str, timeout: int):
    """メインループから分離して実行されるブロッキングPing関数"""
    try:
        response = requests.head(ping_url, timeout=timeout)
        response.raise_for_status()
        logging.debug(f"✅ Self-ping successful (Threaded). Status: {response.status_code}")
        return True
    except requests.exceptions.RequestException as e:
        logging.warning(f"❌ Self-ping failed (Threaded, {type(e).__name__}): {e}. Retrying.")
        return False

async def self_ping_task(interval: int = PING_INTERVAL):
    """RenderのIdleシャットダウンを防ぐためのタスク。ブロッキングなPingをExecutorで実行。"""
    render_url = os.environ.get('RENDER_EXTERNAL_URL')
    if not render_url:
        logging.warning("⚠️ RENDER_EXTERNAL_URL環境変数が設定されていません。自己Pingは無効です。")
        return
    logging.info(f"🟢 自己Pingタスクを開始します (インターバル: {interval}秒, T/O: {PING_TIMEOUT}秒, 方式: Threaded Head)。URL: {render_url}")
    if not render_url.startswith('http'):
        render_url = f"https://{render_url}"
    ping_url = render_url.rstrip('/') + '/' 
    loop = asyncio.get_event_loop()
    while True:
        await loop.run_in_executor(
            None, 
            lambda: blocking_ping(ping_url, PING_TIMEOUT)
        )
        await asyncio.sleep(interval)
        
# ... (中略：メインループ、FastAPI関連関数はv8.9.5から変更なし) ...

async def main_loop():
    """BOTのメイン実行ループ。分析、クライアント切り替え、通知を行う。"""
    global LAST_UPDATE_TIME, CURRENT_MONITOR_SYMBOLS, NOTIFIED_SYMBOLS, NEUTRAL_NOTIFIED_TIME
    global LAST_SUCCESS_TIME, TOTAL_ANALYSIS_ATTEMPTS, TOTAL_ANALYSIS_ERRORS
    global CCXT_CLIENT_NAME, ACTIVE_CLIENT_HEALTH

    loop = asyncio.get_event_loop()
    
    macro_context_data = await loop.run_in_executor(None, get_tradfi_macro_context)
    LAST_UPDATE_TIME = time.time()
    
    await send_test_message()
    asyncio.create_task(self_ping_task(interval=PING_INTERVAL)) 
    asyncio.create_task(instant_price_check_task())

    while True:
        try:
            await asyncio.sleep(MIN_SLEEP_AFTER_IO) 
            current_time = time.time()
            CCXT_CLIENT_NAME = max(ACTIVE_CLIENT_HEALTH, key=ACTIVE_CLIENT_HEALTH.get, default=CCXT_CLIENT_NAMES[0])

            # --- 動的更新フェーズ (10分に一度) ---
            if (current_time - LAST_UPDATE_TIME) >= DYNAMIC_UPDATE_INTERVAL:
                logging.info("==================================================")
                logging.info(f"Apex BOT v9.0.0 分析サイクル開始: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')}")
                macro_context_data = await loop.run_in_executor(None, get_tradfi_macro_context)
                await update_monitor_symbols_dynamically(CCXT_CLIENT_NAME)
                LAST_UPDATE_TIME = current_time
                logging.info(f"監視銘柄数: {len(CURRENT_MONITOR_SYMBOLS)}")
                logging.info("--------------------------------------------------")
            else:
                logging.info(f"🔍 分析開始 (データソース: {CCXT_CLIENT_NAME})")

            # --- メイン分析実行 ---
            candidate_tasks = [generate_signal_candidate(sym, macro_context_data, CCXT_CLIENT_NAME)
                               for sym in CURRENT_MONITOR_SYMBOLS]
            candidates = await asyncio.gather(*candidate_tasks)

            # --- エラーハンドリングとクライアント切り替え ---
            rate_limit_error_found = any(isinstance(c, dict) and c.get('side') in ["RateLimit", "Timeout"] for c in candidates)
            if rate_limit_error_found:
                penalized_time = time.time() - 3600
                logging.error(f"❌ レート制限/タイムアウトエラー発生: クライアント {CCXT_CLIENT_NAME} のヘルスを {penalized_time:.0f} にリセット。")
                ACTIVE_CLIENT_HEALTH[CCXT_CLIENT_NAME] = penalized_time
                next_client = max(ACTIVE_CLIENT_HEALTH, key=ACTIVE_CLIENT_HEALTH.get, default=CCXT_CLIENT_NAMES[0])
                logging.info(f"➡️ 即時クライアント切り替え：次回は {next_client} を優先試行。")
                await asyncio.sleep(5) 
                continue 

            # --- シグナル選定と通知 ---
            valid_candidates_and_neutral = [c for c in candidates if c is not None and c.get('side') not in ["RateLimit", "Timeout"]]
            success_count = len(valid_candidates_and_neutral)
            
            TOTAL_ANALYSIS_ATTEMPTS += len(CURRENT_MONITOR_SYMBOLS)
            TOTAL_ANALYSIS_ERRORS += len(CURRENT_MONITOR_SYMBOLS) - success_count
            if success_count > 0: LAST_SUCCESS_TIME = current_time

            valid_candidates = [c for c in valid_candidates_and_neutral if c.get('side') != "Neutral" and c.get('score', 0) >= 0.50]
            neutral_candidates = [c for c in valid_candidates_and_neutral if c.get('side') == "Neutral"]

            # 1. 最優秀シグナル通知
            if valid_candidates:
                best_signal = max(valid_candidates, key=lambda c: c['score'])
                is_not_recently_notified = current_time - NOTIFIED_SYMBOLS.get(best_signal['symbol'], 0) > 3600
                if is_not_recently_notified:
                    message = format_telegram_message(best_signal)
                    await loop.run_in_executor(None, lambda: send_telegram_html(message, is_emergency=True))
                    NOTIFIED_SYMBOLS[best_signal['symbol']] = current_time
                    logging.info(f"🔔 最優秀候補: {best_signal['symbol']} - {best_signal['side']} (スコア: {best_signal['score']:.4f}) | 通知実行")

            # 2. 強制/中立通知
            time_since_last_neutral = current_time - NEUTRAL_NOTIFIED_TIME
            is_neutral_notify_due = time_since_last_neutral > 1800 

            if is_neutral_notify_due:
                analysis_stats = {"attempts": TOTAL_ANALYSIS_ATTEMPTS, "errors": TOTAL_ANALYSIS_ERRORS, "last_success": LAST_SUCCESS_TIME}
                final_signal_data = None
                
                if neutral_candidates:
                    best_neutral = max(neutral_candidates, key=lambda c: c['confidence'])
                    final_signal_data = {**best_neutral, 'analysis_stats': analysis_stats}
                else:
                    final_signal_data = {
                        "side": "Neutral", "symbol": "FALLBACK", "confidence": 0.0,
                        "regime": "データ不足/レンジ", "is_fallback": True,
                        "macro_context": macro_context_data, "wave_phase": "N/A", "depth_ratio": 0.5, "total_depth": 0,
                        "analysis_stats": analysis_stats, "sentiment_score": 0.5
                    }
                
                neutral_msg = format_telegram_message(final_signal_data)
                NEUTRAL_NOTIFIED_TIME = current_time
                await loop.run_in_executor(None, lambda: send_telegram_html(neutral_msg, is_emergency=False))
                logging.info("➡️ 強制/中立通知を実行しました。")

            await asyncio.sleep(LOOP_INTERVAL)

        except asyncio.CancelledError:
            logging.warning("バックグラウンドタスクがキャンセルされました。")
            break
        except Exception as e:
            logging.error(f"メインループで予期せぬエラーが発生しました: {type(e).__name__}: {e}。{LOOP_INTERVAL}秒後に再試行します。")
            await asyncio.sleep(LOOP_INTERVAL)

def format_price_utility(price, symbol):
    """価格のフォーマットヘルパー"""
    if symbol in ["BTC", "ETH"]: return f"{price:,.2f}"
    return f"{price:,.4f}"

def format_instant_message(symbol: str, side: str, change_pct: float, window_min: int, latest_price: float, oldest_price: float) -> str:
    """突発変動検知用の緊急通知メッセージを整形"""
    icon = "🔥🚨🔥" if change_pct > 0 else "💥📉💥"
    change_sign = "+" if change_pct > 0 else ""
    format_price = lambda p: format_price_utility(p, symbol)

    return (
        f"{icon} <b>{symbol} - 突発変動 {side} 🚨</b> {icon}\n"
        f"-----------------------------------------\n"
        f"• **変動率**: <b>{change_sign}{change_pct:.2f}%</b>\n"
        f"• **期間**: 過去 {window_min} 分間\n"
        f"📉 <b>価格情報</b>:\n"
        f"  - <b>現在価格</b>: <code>${format_price(latest_price)}</code>\n"
        f"  - <b>開始価格</b>: <code>${format_price(oldest_price)}</code>\n"
        f"  - <b>検知時刻</b>: {datetime.now(JST).strftime('%H:%M:%S')} JST\n"
        f"<b>【BOTの判断】: 極端なボラティリティの発生。市場の動向を即座に確認してください。</b>"
    )

app = FastAPI()

@app.on_event("startup")
async def startup_event():
    logging.info("Starting Apex BOT Web Service (v9.0.0 - Liquidity & Regime Enhanced)...")
    initialize_ccxt_client()
    port = int(os.environ.get("PORT", 8000))
    logging.info(f"Web service attempting to bind to port: {port}")
    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    for client in CCXT_CLIENTS_DICT.values():
        if client:
            await client.close()
    logging.info("CCXT Clients closed during shutdown.")

@app.get("/", include_in_schema=False)
@app.head("/", include_in_schema=False) 
async def read_root(request: Request):
    monitor_info = ", ".join(CURRENT_MONITOR_SYMBOLS[:5]) + f"...({len(CURRENT_MONITOR_SYMBOLS)} total)"
    last_health_time = ACTIVE_CLIENT_HEALTH.get(CCXT_CLIENT_NAME, 0)
    last_health_str = datetime.fromtimestamp(last_health_time).strftime('%H:%M:%S') if last_health_time > 0 else "N/A"
    response_data = {
        "status": "Running",
        "service": "Apex BOT v9.0.0 (Liquidity & Regime Enhanced)",
        "monitoring_base": CCXT_CLIENT_NAME,
        "client_health": f"Last Success: {last_health_str}",
        "monitored_symbols": monitor_info,
        "analysis_interval_s": LOOP_INTERVAL,
        "last_analysis_attempt": datetime.fromtimestamp(LAST_UPDATE_TIME).strftime('%H:%M:%S') if LAST_UPDATE_TIME > 0 else "N/A",
    }
    if request.method == "HEAD":
        return JSONResponse(content={}, headers={"Content-Length": "0"})
    return response_data
