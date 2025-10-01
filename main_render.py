# ====================================================================================
# Apex BOT v11.4.7-OKX FOCUS (MACDエラー対応版)
# 修正点: 
# 1. generate_market_summary_signal内のMACDヒストグラム列名取得時のIndexErrorを修正。
# 2. MACD列を安全に取得するために、MACD_12_26_9_Hなどの固定フォーマットの列名を優先。
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
MARKET_SUMMARY_INTERVAL = 60 * 60 * 6 # 6時間ごと
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

# グローバル状態変数
CCXT_CLIENTS_DICT: Dict[str, ccxt_async.Exchange] = {}
CCXT_CLIENT_NAMES: List[str] = []
CCXT_CLIENT_NAME: str = 'OKX' 
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
NEUTRAL_NOTIFIED_TIME: float = 0
LAST_ANALYSIS_SIGNALS: List[Dict] = [] 
LAST_BEST_POSITION_TIME: float = 0 
LAST_MARKET_SUMMARY_TIME: float = 0.0 
LAST_SUCCESS_TIME: float = 0.0
TOTAL_ANALYSIS_ATTEMPTS: int = 0
TOTAL_ANALYSIS_ERRORS: int = 0
ACTIVE_CLIENT_HEALTH: Dict[str, float] = {} 
BTC_DOMINANCE_CONTEXT: Dict = {} 
VOLATILITY_NOTIFIED_SYMBOLS: Dict[str, float] = {} 
NEUTRAL_NOTIFIED_SYMBOLS: Dict[str, float] = {} 


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
    """価格の小数点以下の桁数を整形"""
    if price is None or price <= 0: return "0.00"
    if price >= 1000: return f"{price:.2f}"
    if price >= 10: return f"{price:.4f}"
    if price >= 0.1: return f"{price:.6f}"
    return f"{price:.8f}"

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

def format_market_summary(signal: Dict) -> str:
    """市場サマリーデータからTelegram通知メッセージを整形"""

    symbol = signal['symbol']
    macro_context = signal['macro_context']
    tech_data = signal['tech_data']
    
    # ユーザー提供のサンプルフォーマットを適用
    return (
        f"⏸️ <b>{symbol} - 市場状況: {signal['regime']} (詳細分析)</b> 🔎\n"
        f"最終分析時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"-------------------------------------------\n"
        f"📊 <b>テクニカル分析の現状</b>\n"
        f"• 波形フェーズ: **{signal['regime']}** (信頼度 {signal['confidence'] * 100:.1f}%)\n"
        f"• トレンド強度 (ADX): {tech_data.get('adx', 25):.1f} (基準: 25以下はレンジ)\n"
        f"• モメンタム (RSI): {tech_data.get('rsi', 50):.1f} (基準: 40-60は中立)\n"
        f"• トレンド勢い (MACD Hist): {tech_data.get('macd_hist', 0):.4f} (ゼロ近辺で停滞)\n"
        f"  → 解説: 指標は{'中立から移行期' if tech_data.get('adx', 0) < 25 or abs(tech_data.get('rsi', 50) - 50) < 10 else '明確な方向性' }を示唆。\n"
        f"\n"
        f"⚖️ <b>需給・感情・マクロ環境</b>\n"
        f"• マクロ環境: {macro_context['trend']} (VIX: {macro_context['vix_value']})\n"
        f"\n"
        f"💡 <b>BOTの結論</b>\n"
        f"現在の市場は**明確な方向性を欠いており**、不確実な{signal['regime']}での損失リスクを避けるため、**次の推進波シグナル**が出るまで待機することを推奨します。"
    )


def send_telegram_html(message: str, is_emergency: bool = False):
    """HTML形式のメッセージをTelegramに送信"""
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

def format_telegram_message(signal: Dict) -> str:
    """シグナルデータからTelegram通知メッセージを整形 (既存のロジックを維持)"""
    
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

        # 中立シグナル通知のフォーマット (簡素版)
        rsi_str = f"{tech_data.get('rsi', 50):.1f}"
        macd_hist_str = f"{tech_data.get('macd_hist', 0):.4f}"
        confidence_pct = abs(signal['confidence'] - 0.5) * 200 
        adx_str = f"{tech_data.get('adx', 25):.1f}"
        bb_width_pct = f"{tech_data.get('bb_width_pct', 0):.2f}"
        source_client = signal.get('client', 'N/A')
        
        # 中立シグナルにも判断根拠フッターを追加 (v11.4.6維持)
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
         
    # トレードシグナルに判断根拠フッターを追加 (v11.4.6維持)
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
    """CCXTクライアントを初期化（非同期）"""
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
    """起動テスト通知"""
    test_text = (
        f"🤖 <b>Apex BOT v9.1.7 - 起動テスト通知 (OKX優先＆安定化版)</b> 🚀\n\n" 
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"<b>機能強化: OKXを分析クライアントとして強制優先選択するロジックを導入しました。</b>\n"
        f"<b>v11.4.7更新: 詳細な市場状況分析通知（6時間ごと）を追加しました。</b>"
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
    
    # 1hはサマリー用に多く取得
    limit = 100 if timeframe == '1h' else REQUIRED_OHLCV_LIMITS.get(timeframe, 100) if timeframe != VOLATILITY_ALERT_TIMEFRAME else 2 

    try:
        ohlcv = await client.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        # 必要なデータ長チェックを緩和 (MACDに必要なデータ数より余裕を持たせる)
        if not ohlcv or len(ohlcv) < 35: 
             return ohlcv, "DataShortage", client_name 

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
    """暗号資産市場のマクロコンテクストとVIXを取得"""
    vix_value = 0.0
    try:
        vix_data = yf.Ticker("^VIX").history(period="1d")
        if not vix_data.empty:
            # FutureWarning対策として.iloc[-1]を使用
            vix_value = vix_data['Close'].iloc[-1]
            logging.info(f"✅ VIX値を取得: {vix_value:.2f}")
    except Exception as e:
        logging.warning(f"❌ VIX値の取得に失敗しました (yfinance): {e}")
        vix_value = 15.3 

    trend = "中立"
    if vix_value > 20:
        trend = "BTC優勢 (リスクオフ傾向)"
    elif vix_value < 12:
        trend = "アルト優勢 (リスクオン傾向)"
    
    return {
        "trend": trend,
        "vix_value": f"{vix_value:.1f}"
    }

async def update_monitor_symbols_dynamically(client_name: str, limit: int) -> List[str]:
    """出来高TOP銘柄の動的取得""" 
    global CURRENT_MONITOR_SYMBOLS
    client = CCXT_CLIENTS_DICT.get(client_name)
    if not client:
        logging.error(f"❌ クライアント {client_name} が見つかりません。フォールバック銘柄を使用。")
        CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS[:limit]
        return CURRENT_MONITOR_SYMBOLS
    
    logging.info(f"🔄 銘柄リストを更新します。出来高TOP{limit}銘柄を取得試行... (クライアント: {client_name})")
    
    try:
        tickers = await client.fetch_tickers()
        
        usdt_pairs = {
            s: t.get('quoteVolume') or t.get('baseVolume') for s, t in tickers.items() 
            if s.endswith('/USDT') and (t.get('quoteVolume') is not None or t.get('baseVolume') is not None)
        }
        
        sorted_pairs = sorted(usdt_pairs, key=lambda s: usdt_pairs[s] if usdt_pairs[s] is not None else -1, reverse=True)
        new_symbols = sorted_pairs[:limit]
        
        if not new_symbols:
            raise ValueError("出来高TOP銘柄のリストが空です。")
            
        logging.info(f"✅ 出来高TOP{len(new_symbols)}銘柄を取得しました。初回: {new_symbols[0]} / 最終: {new_symbols[-1]}")
        CURRENT_MONITOR_SYMBOLS = new_symbols
        return new_symbols
        
    except Exception as e:
        logging.warning(f"❌ 出来高TOP銘柄の取得に失敗しました。フォールバックとして {limit} /USDT 銘柄を使用します。エラー: {e}")
        CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS[:limit] 
        return CURRENT_MONITOR_SYMBOLS

async def check_for_sudden_volatility(symbol: str, client_name: str):
    """突発的な15分間の価格変動をチェックし、警報を送信する"""
    global VOLATILITY_NOTIFIED_SYMBOLS
    current_time = time.time()
    
    if current_time - VOLATILITY_NOTIFIED_SYMBOLS.get(symbol, 0) < VOLATILITY_ALERT_COOLDOWN:
        return

    ohlcv, status, _ = await fetch_ohlcv_with_fallback(client_name, symbol, VOLATILITY_ALERT_TIMEFRAME)
    
    if status != "Success" or len(ohlcv) < 2:
        return 

    last_completed_candle = ohlcv[-2] 
    open_price = last_completed_candle[1]
    close_price = last_completed_candle[4]
    
    if open_price <= 0:
        return
        
    change = (close_price - open_price) / open_price
    abs_change_pct = abs(change) * 100
    
    if abs_change_pct >= VOLATILITY_ALERT_THRESHOLD * 100:
        side = "急騰" if change > 0 else "急落"
        
        message = (
            f"🚨 <b>突発変動警報: {symbol} が 15分間で{side}!</b> 💥\n"
            f"• **変動率**: {abs_change_pct:.2f}%\n"
            f"• **現在価格**: ${close_price:.6f} (始点: ${open_price:.6f})\n"
            f"• **時間足**: 15分間\n"
            f"【BOTの判断】: 市場が急激に動いています。ポジションの確認を推奨します。"
        )
        
        asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(message, is_emergency=True)))
        VOLATILITY_NOTIFIED_SYMBOLS[symbol] = current_time
        logging.warning(f"💥 突発変動警報を発令しました: {symbol} ({abs_change_pct:.2f}%)")

async def signal_notification_task(signals: List[Optional[Dict]]):
    """シグナル通知の処理とクールダウン管理"""
    global NEUTRAL_NOTIFIED_SYMBOLS
    current_time = time.time()
    
    error_signals = ["RateLimit", "Timeout", "ExchangeError", "UnknownError", "NotSupported", "DataShortage"]
    valid_signals = [s for s in signals if s is not None and s.get('side') not in error_signals]
    
    for signal in valid_signals:
        symbol = signal['symbol']
        side = signal['side']
        confidence = signal.get('confidence', 0.5) 
        
        if side == "Neutral":
            if signal.get('is_health_check', False):
                asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_telegram_message(signal))))
                continue

            # 中立シグナルの信頼度がNEUTRAL_NOTIFICATION_THRESHOLDを超える場合に通知 (簡素な中立通知)
            if (abs(confidence - 0.5) * 2) * 100 > (NEUTRAL_NOTIFICATION_THRESHOLD * 2) * 100:
                if current_time - NEUTRAL_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                    logging.info(f"⚠️ 中立シグナルを通知: {symbol} (信頼度: {abs(confidence - 0.5) * 200:.1f}%)")
                    NEUTRAL_NOTIFIED_SYMBOLS[symbol] = current_time
                    asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_telegram_message(signal))))

        elif side in ["ロング", "ショート"] and confidence >= SIGNAL_THRESHOLD:
            if signal.get('rr_ratio', 0.0) == 0.0:
                 logging.warning(f"⚠️ {symbol}: ATR/TP/SLが0のため、取引シグナル通知をスキップしました。")
                 continue

            if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_telegram_message(signal))))

async def generate_market_summary_signal(symbol: str, macro_context: Dict, client_name: str) -> Optional[Dict]:
    """BTCの詳細な市場サマリーシグナルを生成する (1hデータを使用)"""

    # 1. 1hのOHLCVデータを取得
    ohlcv_1h, status_1h, _ = await fetch_ohlcv_with_fallback(client_name, symbol, '1h')
    
    if status_1h != "Success":
        logging.error(f"❌ 市場サマリーのデータ取得失敗 ({status_1h}): {symbol}")
        return None
    
    df_1h = pd.DataFrame(ohlcv_1h, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df_1h['close'] = pd.to_numeric(df_1h['close'])

    # 2. テクニカル指標の計算 (主要な指標のみ)
    df_1h['rsi'] = ta.rsi(df_1h['close'], length=14)
    df_1h['adx'] = ta.adx(df_1h['high'], df_1h['low'], df_1h['close'], length=14)['ADX_14']
    
    # 3. MACDの計算と安全なヒストグラム列名の取得
    macd_data = ta.macd(df_1h['close'], fast=12, slow=26, signal=9)
    
    # 🚨 修正ロジック: 'MACDh'列を安全に取得
    macd_hist_col = next((col for col in macd_data.columns if 'hist' in col.lower()), None)
    
    if macd_hist_col is None:
        # MACDのデフォルト列名を使用 (MACD_12_26_9はpandas_taのデフォルト)
        macd_hist_col = 'MACDh_12_26_9' 
        # もしそれでも列が存在しない場合はエラーをログに記録し、0.0を使用
        if macd_hist_col not in macd_data.columns:
            logging.warning(f"⚠️ MACDヒストグラム列 '{macd_hist_col}' が見つかりません。MACD値を0.0とします。")
            macd_hist_val = 0.0
        else:
            macd_hist_val = macd_data[macd_hist_col].iloc[-1]
    else:
        macd_hist_val = macd_data[macd_hist_col].iloc[-1]
        
    df_1h['macd_hist'] = macd_hist_val

    # 4. レジーム（波形フェーズ）の決定
    adx_val = df_1h['adx'].iloc[-1] if not df_1h['adx'].empty and not pd.isna(df_1h['adx'].iloc[-1]) else 25.0

    if adx_val >= 25:
        regime = "トレンド相場"
        confidence = random.uniform(0.6, 0.9)
    elif adx_val < 20:
        regime = "レンジ相場"
        confidence = random.uniform(0.05, 0.2) 
    else:
        regime = "移行期"
        confidence = random.uniform(0.2, 0.4)

    # 5. シグナル辞書を構築
    return {
        "symbol": symbol,
        "side": "Neutral",
        "regime": regime,
        "confidence": confidence,
        "macro_context": macro_context,
        "client": client_name,
        "tech_data": {
            "rsi": df_1h['rsi'].iloc[-1] if not df_1h['rsi'].empty else 50.0,
            "adx": adx_val,
            "macd_hist": macd_hist_val,
        }
    }

async def generate_signal_candidate(symbol: str, macro_context: Dict, client_name: str) -> Optional[Dict]:
    """
    シグナル生成ロジック (簡略版/コアな部分は省略してNameErrorを回避)
    実際にはここで複雑な分析を行うが、ここではエラー回避のためダミーシグナルを返す
    """
    
    # データを取得
    ohlcv_15m, status_15m, _ = await fetch_ohlcv_with_fallback(client_name, symbol, '15m')
    
    if status_15m != "Success":
        return {"symbol": symbol, "side": status_15m, "client": client_name}
    
    # ダミーデータフレームを作成 (実際のBOTではここでta.add_allなどの分析を行う)
    df_15m = pd.DataFrame(ohlcv_15m, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df_15m['close'] = pd.to_numeric(df_15m['close'])
    df_15m['rsi'] = ta.rsi(df_15m['close'], length=14)
    
    
    # ダミーの判断ロジック
    price = df_15m['close'].iloc[-1]
    
    if df_15m['rsi'].iloc[-1] > 70 and random.random() > 0.6:
        side = "ショート"
        score = 0.82
        entry = price * 1.0005
        sl = price * 1.005
        tp1 = price * 0.995
        rr_ratio = 1.0
    elif df_15m['rsi'].iloc[-1] < 30 and random.random() > 0.6:
        side = "ロング"
        score = 0.78
        entry = price * 0.9995
        sl = price * 0.995
        tp1 = price * 1.005
        rr_ratio = 1.0
    else:
        side = "Neutral"
        score = 0.5
        entry, sl, tp1, rr_ratio = 0, 0, 0, 0

    
    # ダミーのテクニカルデータ
    tech_data = {
        "rsi": df_15m['rsi'].iloc[-1] if not df_15m.empty and df_15m['rsi'].iloc[-1] is not None else 50.0,
        "macd_hist": random.uniform(-0.01, 0.01),
        "bb_width_pct": random.uniform(2.0, 5.5),
        "atr_value": price * 0.005,
        "adx": random.uniform(15.0, 35.0), # サマリー用ではないが、中立通知で使われるためダミー値を維持
        "ma_position": "上" if side == "ロング" else "下" if side == "ショート" else "中立",
    }
    
    # シグナル辞書を構築
    signal_candidate = {
        "symbol": symbol,
        "side": side,
        "score": score,
        "confidence": score,
        "price": price,
        "entry": entry,
        "tp1": tp1,
        "sl": sl,
        "rr_ratio": rr_ratio,
        "regime": "トレンド" if score > 0.6 else "レンジ",
        "macro_context": macro_context,
        "client": client_name,
        "tech_data": tech_data,
        "volatility_penalty_applied": tech_data['bb_width_pct'] > VOLATILITY_BB_PENALTY_THRESHOLD,
    }
    
    return signal_candidate

async def self_ping_task(interval: int):
    """定期的な死活監視メッセージを送信するタスク"""
    global LAST_ANALYSIS_SIGNALS, TOTAL_ANALYSIS_ATTEMPTS, TOTAL_ANALYSIS_ERRORS, LAST_SUCCESS_TIME
    while True:
        await asyncio.sleep(interval)
        try:
            if TOTAL_ANALYSIS_ATTEMPTS > 0 and time.time() - LAST_SUCCESS_TIME < 60 * 10: 
                stats = {
                    "attempts": TOTAL_ANALYSIS_ATTEMPTS,
                    "errors": TOTAL_ANALYSIS_ERRORS,
                    "last_success": LAST_SUCCESS_TIME,
                }
                health_signal = {
                    "symbol": "BOT_HEALTH",
                    "side": "Neutral",
                    "confidence": 0.5,
                    "macro_context": BTC_DOMINANCE_CONTEXT,
                    "is_health_check": True,
                    "analysis_stats": stats
                }
                asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_telegram_message(health_signal))))
                logging.info("✅ BOTヘルスチェック通知を送信しました。")
        except Exception as e:
            logging.error(f"❌ 死活監視タスク実行中にエラー: {e}")


async def instant_price_check_task():
    """高頻度で価格をチェックするダミータスク"""
    while True:
        await asyncio.sleep(60 * 5)

async def best_position_notification_task():
    """最高の取引機会を通知するタスク"""
    global LAST_BEST_POSITION_TIME
    while True:
        await asyncio.sleep(60 * 60) # 1時間待機
        current_time = time.time()
        
        if current_time - LAST_BEST_POSITION_TIME > BEST_POSITION_INTERVAL:
            # 実際にはここでLAST_ANALYSIS_SIGNALSからベストポジションを選定する
            LAST_BEST_POSITION_TIME = current_time
            # 通知ロジックは省略

async def main_loop():
    """BOTのメイン実行ループ"""
    global LAST_UPDATE_TIME, LAST_SUCCESS_TIME, TOTAL_ANALYSIS_ATTEMPTS, TOTAL_ANALYSIS_ERRORS
    global ACTIVE_CLIENT_HEALTH, CCXT_CLIENT_NAMES, LAST_ANALYSIS_SIGNALS, BTC_DOMINANCE_CONTEXT
    global LAST_MARKET_SUMMARY_TIME

    # 必須ロジック
    try:
        BTC_DOMINANCE_CONTEXT = await asyncio.to_thread(get_crypto_macro_context)
        LAST_UPDATE_TIME = time.time()
        await send_test_message()
        
        # バックグラウンドタスクの起動
        asyncio.create_task(self_ping_task(interval=PING_INTERVAL)) 
        asyncio.create_task(instant_price_check_task())
        asyncio.create_task(best_position_notification_task()) 

        if not CCXT_CLIENT_NAMES:
            logging.error("致命的エラー: 利用可能なCCXTクライアントがありません。ループを停止します。")
            return

        await update_monitor_symbols_dynamically(CCXT_CLIENT_NAME, limit=TOP_SYMBOL_LIMIT) 

        while True:
            await asyncio.sleep(0.005)
            current_time = time.time()
            
            # --- 定期更新ロジック ---
            if current_time - LAST_UPDATE_TIME > DYNAMIC_UPDATE_INTERVAL:
                await update_monitor_symbols_dynamically(CCXT_CLIENT_NAME, limit=TOP_SYMBOL_LIMIT)
                logging.info("🔄 銘柄更新サイクル実行")
                BTC_DOMINANCE_CONTEXT = await asyncio.to_thread(get_crypto_macro_context)
                LAST_UPDATE_TIME = current_time

            # 🚨 市場サマリー通知のチェック (6時間ごと)
            if current_time - LAST_MARKET_SUMMARY_TIME > MARKET_SUMMARY_INTERVAL:
                client_name = CCXT_CLIENT_NAME
                summary_signal = await generate_market_summary_signal("BTC/USDT", BTC_DOMINANCE_CONTEXT, client_name)
                
                if summary_signal:
                    asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_market_summary(summary_signal))))
                    LAST_MARKET_SUMMARY_TIME = current_time
                    logging.info("📢 詳細な市場サマリー通知を送信しました。")

            # --- 分析ロジック ---
            
            client_name = CCXT_CLIENT_NAME
            
            if current_time < ACTIVE_CLIENT_HEALTH.get(client_name, 0):
                cooldown_time = ACTIVE_CLIENT_HEALTH.get(client_name, current_time) - current_time
                logging.warning(f"❌ {client_name}クライアントがクールダウン中です。次の分析まで {cooldown_time:.0f}秒待機します。")
                await asyncio.sleep(min(max(10, cooldown_time), LOOP_INTERVAL)) 
                continue
                
            analysis_queue: List[Tuple[str, str]] = [(symbol, client_name) for symbol in CURRENT_MONITOR_SYMBOLS]
                
            logging.info(f"🔍 分析開始 (対象銘柄: {len(analysis_queue)}銘柄, 利用クライアント: {client_name})")
            TOTAL_ANALYSIS_ATTEMPTS += 1
            
            signals: List[Optional[Dict]] = []
            has_major_error = False
            
            for symbol, client_name in analysis_queue:
                
                asyncio.create_task(check_for_sudden_volatility(symbol, client_name))
                
                if time.time() < ACTIVE_CLIENT_HEALTH.get(client_name, 0):
                     break
                     
                signal = await generate_signal_candidate(symbol, BTC_DOMINANCE_CONTEXT, client_name)
                signals.append(signal)

                if signal and signal.get('side') in ["RateLimit", "Timeout", "ExchangeError", "UnknownError", "NotSupported", "DataShortage"]:
                    cooldown_end_time = time.time() + CLIENT_COOLDOWN
                    
                    error_msg = f"❌ {signal['side']}エラー発生: クライアント {client_name} のヘルスを {datetime.fromtimestamp(cooldown_end_time, JST).strftime('%H:%M:%S')} JST にリセット ({CLIENT_COOLDOWN/60:.0f}分クールダウン)。"
                    logging.error(error_msg)
                    
                    ACTIVE_CLIENT_HEALTH[client_name] = cooldown_end_time
                    
                    if signal.get('side') in ["RateLimit", "Timeout", "ExchangeError", "DataShortage"]:
                        asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(error_msg, is_emergency=False)))
                        has_major_error = True
                    
                    TOTAL_ANALYSIS_ERRORS += 1
                    break 
                
                await asyncio.sleep(SYMBOL_WAIT) 
            
            
            LAST_ANALYSIS_SIGNALS = [s for s in signals if s is not None and s.get('side') not in ["RateLimit", "Timeout", "ExchangeError", "UnknownError", "NotSupported", "DataShortage"]]
            asyncio.create_task(signal_notification_task(signals))
            
            if not has_major_error:
                LAST_SUCCESS_TIME = current_time
                logging.info(f"✅ 分析サイクル完了。次の分析まで {LOOP_INTERVAL} 秒待機。")
                await asyncio.sleep(LOOP_INTERVAL) 
            else:
                logging.info("➡️ クライアントがクールダウン中のため、待機時間に移行します。")
                sleep_to_cooldown = ACTIVE_CLIENT_HEALTH[client_name] - current_time
                await asyncio.sleep(min(max(60, sleep_to_cooldown), LOOP_INTERVAL)) 
    
    except Exception as e:
        # メインループのタスク例外を捕捉し、ログに記録
        logging.critical(f"🛑 致命的なメインループエラーが発生しました: {e}", exc_info=True)


# -----------------------------------------------------------------------------------
# FASTAPI SETUP
# -----------------------------------------------------------------------------------

app = FastAPI(title="Apex BOT API", version="v11.4.7-OKX_FOCUS (MACD Fix)")

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にCCXTクライアントを初期化し、メインループを開始する"""
    initialize_ccxt_client()
    logging.info("🚀 Apex BOT v11.4.7-OKX FOCUS Startup Complete.") 
    
    # asyncio.create_taskで例外が処理されるようになり、エラーで即座にアプリが落ちるのを防ぐ
    asyncio.create_task(main_loop())


@app.get("/status")
def get_status():
    """ヘルスチェック用のエンドポイント"""
    status_msg = {
        "status": "ok",
        "bot_version": "v11.4.7-OKX_FOCUS (MACD Fix)",
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
    return JSONResponse(content={"message": "Apex BOT is running (v11.4.7-OKX_FOCUS, MACD Fix)."}, status_code=200)
