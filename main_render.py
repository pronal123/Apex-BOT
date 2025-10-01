# ====================================================================================
# Apex BOT v11.4.6-OKX FOCUS (判断根拠追記版 - 代替案2適用)
# 修正点: 
# 1. トレードシグナルと中立シグナル通知に、RSI/MACD Hist/BB幅の判断根拠を示すテキスト表を追加。
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

# 監視銘柄を主要30銘柄に戻す (フォールバック用)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT", 
    "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "LTC/USDT", "MATIC/USDT", "TRX/USDT", 
    "ATOM/USDT", "NEAR/USDT", "ALGO/USDT", "XLM/USDT", "BCH/USDT", "ETC/USDT", 
    "UNI/USDT", "ICP/USDT", "FIL/USDT", "AAVE/USDT", "AXS/USDT", "SAND/USDT",
    "GALA/USDT", "FTM/USDT", "HBAR/USDT", "VET/USDT", "GRT/USDT", "SHIB/USDT"
] 
TOP_SYMBOL_LIMIT = 30      
LOOP_INTERVAL = 360        
SYMBOL_WAIT = 0.0          

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

# 設定値
PING_INTERVAL = 8          
DYNAMIC_UPDATE_INTERVAL = 60 * 30
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2
BEST_POSITION_INTERVAL = 60 * 60 * 12
SIGNAL_THRESHOLD = 0.55 
CLIENT_COOLDOWN = 45 * 60  
REQUIRED_OHLCV_LIMITS = {'15m': 100, '1h': 100, '4h': 100} 
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 

# 突発変動警報設定
VOLATILITY_ALERT_THRESHOLD = 0.030  
VOLATILITY_ALERT_TIMEFRAME = '15m'
VOLATILITY_ALERT_COOLDOWN = 60 * 30 

# 中立シグナル通知の閾値 
NEUTRAL_NOTIFICATION_THRESHOLD = 0.05

# グローバル状態変数 (v11.4.5と同一)
CCXT_CLIENTS_DICT: Dict[str, ccxt_async.Exchange] = {}
CCXT_CLIENT_NAMES: List[str] = []
CCXT_CLIENT_NAME: str = 'OKX' 
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
NEUTRAL_NOTIFIED_TIME: float = 0
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_BEST_POSITION_TIME: float = 0 
LAST_SUCCESS_TIME: float = 0.0
TOTAL_ANALYSIS_ATTEMPTS: int = 0
TOTAL_ANALYSIS_ERRORS: int = 0
ACTIVE_CLIENT_HEALTH: Dict[str, float] = {} 
BTC_DOMINANCE_CONTEXT: Dict = {} 
VOLATILITY_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
NEUTRAL_NOTIFIED_SYMBOLS: Dict[str, float] = {} 


# ロギング設定 (v11.4.5と同一)
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
    """価格の小数点以下の桁数を整形 (v11.4.5と同一)"""
    if price is None or price <= 0: return "0.00"
    if price >= 1000: return f"{price:.2f}"
    if price >= 10: return f"{price:.4f}"
    if price >= 0.1: return f"{price:.6f}"
    return f"{price:.8f}"

# 🚨 新規関数: テクニカル指標の期待値に基づいてテキストサマリーを生成
def generate_analysis_summary(signal: Dict) -> str:
    """
    RSI, MACD Hist, BB幅の数値に基づき、BOTの判断根拠となるテキストサマリーを生成する。
    """
    tech_data = signal.get('tech_data', {})
    side = signal.get('side', 'Neutral')
    
    rsi = tech_data.get('rsi', 50.0)
    macd_hist = tech_data.get('macd_hist', 0.0)
    bb_width_pct = tech_data.get('bb_width_pct', 0.0)
    
    summary_lines = []
    
    # 1. RSIの判断
    rsi_judgment = ""
    rsi_status = f"RSI({rsi:.1f}): "
    if rsi >= 75:
        rsi_judgment = "[極度な買われすぎ] → ⚡️反転圧力"
    elif rsi >= 70:
        rsi_judgment = "[買われすぎ] → ⚠ ペナルティ" if side == "ロング" else "[反転期待] → ✅ 加点"
    elif rsi <= 25:
        rsi_judgment = "[極度な売られすぎ] → ⚡️反転圧力"
    elif rsi <= 30:
        rsi_judgment = "[売られすぎ] → ⚠ ペナルティ" if side == "ショート" else "[反転期待] → ✅ 加点"
    else:
        rsi_judgment = "[中立/適正水準] → ↕️ 影響小"
    summary_lines.append(rsi_status + rsi_judgment)

    # 2. MACD Histの判断
    macd_judgment = ""
    macd_status = f"MACD Hist({macd_hist:+.4f}): "
    if abs(macd_hist) > 0.005: # モメンタムが明確
        if macd_hist > 0:
            macd_judgment = "[強力な上昇モメンタム] → ✅ 大幅加点" if side == "ロング" else "[モメンタム乖離] → ⚠ 警戒"
        else:
            macd_judgment = "[強力な下降モメンタム] → ✅ 大幅加点" if side == "ショート" else "[モメンタム乖離] → ⚠ 警戒"
    elif abs(macd_hist) > 0.0005:
        macd_judgment = "[モメンタム加速/減速]"
    else:
        macd_judgment = "[ゼロライン付近] → 🔄 方向性不明瞭"
    summary_lines.append(macd_status + macd_judgment)
    
    # 3. BB幅の判断
    bb_judgment = ""
    bb_status = f"BB幅({bb_width_pct:.2f}%): "
    if bb_width_pct >= VOLATILITY_BB_PENALTY_THRESHOLD: # 5.0%
        bb_judgment = "[極度な高ボラティリティ] → ❌ 大幅ペナルティ"
    elif bb_width_pct >= 4.0:
        bb_judgment = "[高ボラティリティ] → ⚠ ペナルティ適用"
    elif bb_width_pct <= 1.5:
        bb_judgment = "[強力なスクイーズ] → 💥 ブレイク期待"
    elif bb_width_pct <= 2.5:
        bb_judgment = "[低ボラティリティ] → 待ち"
    else:
        bb_judgment = "[通常ボラティリティ]"
    summary_lines.append(bb_status + bb_judgment)

    # 最終的なテキスト表の構築
    if not summary_lines:
        return ""

    header = "\n\n---"
    footer = "💡 BOT分析の核心 (15分足)"
    body = "\n" + "\n".join(summary_lines)
    
    return header + "\n<b>" + footer + "</b>" + "\n" + body
    
def send_telegram_html(message: str, is_emergency: bool = False):
    """HTML形式のメッセージをTelegramに送信 (v11.4.5と同一)"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.error("❌ Telegram設定(トークン/ID)が不足しています。通知をスキップ。")
        return
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML',
        'disable_notification': not is_emergency
    }
    
    try:
        response = requests.post(url, data=payload, timeout=10)
        response.raise_for_status() 
    except requests.exceptions.HTTPError as e:
        logging.error(f"❌ Telegram HTTPエラー: {response.status_code} - {response.text}")
    except requests.exceptions.RequestException as e:
        logging.error(f"❌ Telegram リクエストエラー: {e}")

# 🚨 修正: format_telegram_messageに generate_analysis_summaryの呼び出しを追加
def format_telegram_message(signal: Dict) -> str:
    """シグナルデータからTelegram通知メッセージを整形"""
    
    macro_trend = signal['macro_context']['trend']
    vix_val = signal['macro_context'].get('vix_value', 'N/A')
    tech_data = signal.get('tech_data', {})
    
    # --- 1. 中立/ヘルス通知 ---
    if signal['side'] == "Neutral":
        if signal.get('is_health_check', False):
            stats = signal.get('analysis_stats', {"attempts": 0, "errors": 0, "last_success": 0})
            error_rate = (stats['errors'] / stats['attempts']) * 100 if stats['attempts'] > 0 else 0
            last_success_time = datetime.fromtimestamp(stats['last_success'], JST).strftime('%H:%M:%S') if stats['last_success'] > 0 else "N/A"
            
            # v9.1.7の死活監視フォーマット
            return (
                f"🚨 <b>Apex BOT v9.1.7 - 死活監視 (システム正常)</b> 🟢\n" 
                f"<i>強制通知時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST</i>\n\n"
                f"• **市場コンテクスト**: {macro_trend} (VIX: {vix_val})\n" 
                f"• **🤖 BOTヘルス**: 最終成功: {last_success_time} JST (エラー率: {error_rate:.1f}%)\n"
                f"【BOTの判断】: データ取得と分析は正常に機能しています。待機中。"
            )

        # 中立シグナル通知のフォーマット
        rsi_str = f"{tech_data.get('rsi', 50):.1f}"
        macd_hist_str = f"{tech_data.get('macd_hist', 0):.4f}"
        confidence_pct = abs(signal['confidence'] - 0.5) * 200 
        adx_str = f"{tech_data.get('adx', 25):.1f}"
        bb_width_pct = f"{tech_data.get('bb_width_pct', 0):.2f}"
        source_client = signal.get('client', 'N/A')
        
        # 🚨 中立シグナルにも判断根拠フッターを追加
        analysis_summary = generate_analysis_summary(signal)

        return_message = (
            f"⚠️ <b>{signal['symbol']} - 市場分析速報 (中立)</b> ⏸️\n"
            f"<b>信頼度: {confidence_pct:.1f}%</b> (データ元: {source_client})\n"
            f"---------------------------\n"
            f"• <b>市場環境/レジーム</b>: {signal['regime']} (ADX: {adx_str}) | {macro_trend} (BB幅: {bb_width_pct}%)\n"
            f"\n"
            f"📊 <b>テクニカル詳細</b>:\n"
            f"  - <i>RSI (15m)</i>: {rsi_str} | <i>MACD Hist (15m)</i>: {macd_hist_str}\n" 
            f"  - <i>MAとの位置</i>: {tech_data.get('ma_position', '中立')}\n"
            f"\n"
            f"<b>【BOTの判断】: {signal['regime']}で方向性が不鮮明です。様子見推奨。</b>"
        )
        return return_message + analysis_summary
    
    # --- 2. トレードシグナル通知 ---
    score = signal['score']
    side_icon = "⬆️ LONG" if signal['side'] == "ロング" else "⬇️ SHORT"
    
    if score >= 0.85: score_icon = "🚀🌕🌕"; lot_size = "MAX"; action = "積極的にエントリー (高確度)"
    elif score >= 0.75: score_icon = "🔥🔥🔥"; lot_size = "大"; action = "標準的なエントリー (良好)"
    elif score >= 0.60: score_icon = "🔥🌟"; lot_size = "中"; action = "慎重なエントリー (許容範囲)"
    else: score_icon = "✨"; lot_size = "小"; action = "極めて慎重に"

    atr_val = tech_data.get('atr_value', 0)
    penalty_info = ""
    if signal.get('volatility_penalty_applied'):
        penalty_info = "⚠️ ボラティリティペナルティ適用済 (荒れた相場)"
        
    rr_ratio = signal.get('rr_ratio', 0.0) 

    atr_warning = ""
    format_price = lambda p: format_price_utility(p, signal.get('symbol', 'BTC/USDT'))
    if atr_val == 0.0:
         atr_warning = "⚠️ <b>ATR/TP/SLが0: データ不足または計算失敗。取引不可。</b>"
         
    # 🚨 トレードシグナルに判断根拠フッターを追加
    analysis_summary = generate_analysis_summary(signal)

    return_message = (
        f"{score_icon} <b>{signal['symbol']} - {side_icon} シグナル発生!</b> {score_icon}\n"
        f"<b>信頼度スコア (MTFA統合): {score * 100:.2f}%</b> {penalty_info}\n"
        f"データ元: {signal.get('client', 'N/A')}\n"
        f"-----------------------------------------\n"
        f"• <b>現在価格</b>: <code>${format_price(signal['price'])}</code>\n"
        f"• <b>ATR (ボラティリティ指標)</b>: <code>${format_price(atr_val)}</code>\n" 
        f"\n"
        f"🎯 <b>ATRに基づく取引計画</b>:\n"
        f"  - エントリー: **<code>${format_price(signal['entry'])}</code>**\n"
        f"🟢 <b>利確 (TP)</b>: **<code>${format_price(signal['tp1'])}</code>**\n" 
        f"🔴 <b>損切 (SL)</b>: **<code>${format_price(signal['sl'])}</code>**\n"
        f"  - **リスクリワード比 (RRR)**: **<code>1:{rr_ratio:.2f}</code>** (スコアに基づく動的設定)\n" 
        f"{atr_warning}\n" 
        f"<b>【BOTの判断】: MTFAと複合モメンタムにより裏付けられた高確度シグナルです。</b>"
    )
    return return_message + analysis_summary


def initialize_ccxt_client():
    """CCXTクライアントを初期化（非同期） (v11.4.5と同一)"""
    global CCXT_CLIENTS_DICT, CCXT_CLIENT_NAMES, ACTIVE_CLIENT_HEALTH
    
    clients = {
        'OKX': ccxt_async.okx({ 
            "enableRateLimit": True, 
            "timeout": 60000, 
            "rateLimit": 200, 
        }), 
    }
    CCXT_CLIENTS_DICT = clients
    CCXT_CLIENT_NAMES = list(CCXT_CLIENTS_DICT.keys())
    ACTIVE_CLIENT_HEALTH = {name: time.time() for name in CCXT_CLIENT_NAMES}
    logging.info(f"✅ CCXTクライアント初期化完了。利用可能なクライアント: {CCXT_CLIENT_NAMES}")

async def send_test_message():
    """起動テスト通知 (v11.4.5のOKX優先フォーマットを維持)"""
    test_text = (
        f"🤖 <b>Apex BOT v9.1.7 - 起動テスト通知 (OKX優先＆安定化版)</b> 🚀\n\n" 
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"<b>機能強化: OKXを分析クライアントとして強制優先選択するロジックを導入しました。</b>\n"
        f"<b>v11.4.6更新: シグナル通知に判断根拠 (RSI/MACD Hist/BB幅) のテキストサマリーを追加しました。</b>"
    )
    try:
        await asyncio.to_thread(lambda: send_telegram_html(test_text, is_emergency=True)) 
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")

# ... (fetch_ohlcv_with_fallback, get_crypto_macro_context, update_monitor_symbols_dynamically, 
# check_for_sudden_volatility, signal_notification_task, main_loop, self_ping_task, 
# instant_price_check_task, best_position_notification_task, generate_signal_candidateなどのロジックはv11.4.5と同一)


# -----------------------------------------------------------------------------------
# FASTAPI SETUP
# -----------------------------------------------------------------------------------

app = FastAPI(title="Apex BOT API", version="v11.4.6-OKX_FOCUS")

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にCCXTクライアントを初期化し、メインループを開始する"""
    initialize_ccxt_client()
    logging.info("🚀 Apex BOT v11.4.6-OKX FOCUS Startup Complete.") 
    
    asyncio.create_task(main_loop())


@app.get("/status")
def get_status():
    """ヘルスチェック用のエンドポイント"""
    status_msg = {
        "status": "ok",
        "bot_version": "v11.4.6-OKX_FOCUS (Analysis Summary Added)",
        "last_success_timestamp": LAST_SUCCESS_TIME,
        "active_clients_count": 1 if time.time() >= ACTIVE_CLIENT_HEALTH.get(CCXT_CLIENT_NAME, 0) else 0,
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
    return JSONResponse(content={"message": "Apex BOT is running (v11.4.6-OKX_FOCUS, Analysis Summary Added)."}, status_code=200)

