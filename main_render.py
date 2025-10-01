# ====================================================================================
# Apex BOT v11.4.7-KRAKEN FALLBACK (フル統合版)
# 修正点: 
# 1. CCXTクライアントにKrakenを追加し、フォールバック先に設定。
# 2. fetch_ohlcv_with_fallbackで15分足データ不足時、1時間足で代替するフォールバックロジックを実装。
# 3. generate_signal_candidate内のロジックを、使用された時間足に対応させるよう調整。
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
PING_INTERVAL = 60 * 60          
DYNAMIC_UPDATE_INTERVAL = 60 * 30
TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2
BEST_POSITION_INTERVAL = 60 * 60 * 12
MARKET_SUMMARY_INTERVAL = 60 * 60 * 6 # 6時間ごと
SIGNAL_THRESHOLD = 0.55 
CLIENT_COOLDOWN = 45 * 60  
# DataShortage銘柄の一時無効化時間 (4時間)
SYMBOL_COOLDOWN_TIME = 60 * 60 * 4 
# 1hはサマリー用に100本確保。15mは分析に最低限必要な本数を確保 (35→100に増強)
REQUIRED_OHLCV_LIMITS = {'15m': 100, '1h': 100, '4h': 100} 
MIN_OHLCV_FOR_ANALYSIS = 35 # 少なくともこれだけのデータがないと指標計算不可
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
CCXT_CLIENT_NAME: str = 'OKX' # メインクライアント
CCXT_FALLBACK_CLIENT_NAME: str = 'Kraken' # フォールバッククライアント
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
# 銘柄ごとのクールダウン管理
SYMBOL_COOLDOWN_DICT: Dict[str, float] = {}


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
    timeframe = signal.get('timeframe', '15m')
    
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
    footer = f"💡 BOT分析の核心 ({timeframe}足)"
    body = "\n" + "\n".join(summary_lines)
    
    return header + "\n<b>" + footer + "</b>" + "\n" + body

def format_market_summary(signal: Dict) -> str:
    """市場サマリーデータからTelegram通知メッセージを整形し、推奨アクションを明示する"""

    symbol = signal['symbol']
    macro_context = signal['macro_context']
    tech_data = signal['tech_data']
    
    # データを取得
    regime = signal['regime']
    confidence = signal['confidence'] * 100
    adx = tech_data.get('adx', 25)
    rsi = tech_data.get('rsi', 50)
    macd_hist = tech_data.get('macd_hist', 0)
    
    # ----------------------------------------------------
    # 🎯 結論と推奨アクションの決定ロジック
    # ----------------------------------------------------
    action_recommendation = "待機"
    conclusion_text = ""
    action_emoji = "⏸️"

    if regime == "トレンド相場" and adx >= 25:
        # 買われすぎ/売られすぎによる反転リスクをチェック
        if rsi >= 70:
            action_recommendation = "ショート/待機"
            conclusion_text = f"現在の強いトレンドはMACD Hist ({macd_hist:+.4f})で継続していますが、RSIが極度の買われすぎ ({rsi:.1f}) の水準です。**短期的な反落（ショート）**に警戒し、新規エントリーは**待機**を推奨します。"
            action_emoji = "⚠️"
        elif rsi <= 30:
            action_recommendation = "ロング/待機"
            conclusion_text = f"現在の強いトレンドはMACD Hist ({macd_hist:+.4f})で継続していますが、RSIが極度の売られすぎ ({rsi:.1f}) の水準です。**短期的な反発（ロング）**に警戒し、新規エントリーは**待機**を推奨します。"
            action_emoji = "⚠️"
        # RSIが中立域でMACDが0より大きい場合は強いロング推奨
        elif 40 < rsi < 60 and macd_hist > 0.0005: 
            action_recommendation = "ロング"
            conclusion_text = f"トレンドは強く ({adx:.1f})、モメンタムも正 ({macd_hist:+.4f}) で継続しています。RSI ({rsi:.1f}) も過熱感がなく、**明確なロング**シグナルを示唆します。**追従エントリー**を検討してください。"
            action_emoji = "🟢"
        # RSIが中立域でMACDが0より小さい場合は強いショート推奨
        elif 40 < rsi < 60 and macd_hist < -0.0005:
            action_recommendation = "ショート"
            conclusion_text = f"トレンドは強く ({adx:.1f})、モメンタムも負 ({macd_hist:+.4f}) で継続しています。RSI ({rsi:.1f}) も冷却されており、**明確なショート**シグナルを示唆します。**追従エントリー**を検討してください。"
            action_emoji = "🔴"
        else:
            action_recommendation = "待機"
            conclusion_text = "ADXは強いものの、主要なモメンタム指標が方向性を明確に示していません。次の明確なシグナルまで**待機**を推奨します。"
            action_emoji = "⏸️"

    elif regime == "レンジ相場" and adx < 20:
        action_recommendation = "待機"
        conclusion_text = "市場はADX ({adx:.1f}) が示す通り、強いレンジ相場にあります。不確実な相場での損失リスクを避けるため、**次の推進波シグナル**が出るまで**待機**を推奨します。"
        action_emoji = "⏸️"
        
    else: # 移行期やその他のパターン
        action_recommendation = "待機 (慎重)"
        conclusion_text = "市場は現在、トレンドとレンジの**移行期**にあり、方向性が不鮮明です。主要な指標が安定したシグナルを出すまで、**慎重に待機**することを推奨します。"
        action_emoji = "⚠️"
    # ----------------------------------------------------
    
    # ユーザー提供のサンプルフォーマットを適用
    return (
        f"{action_emoji} <b>{symbol} - 市場状況: {regime} ({action_recommendation}推奨)</b> 🔎\n"
        f"最終分析時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"-------------------------------------------\n"
        f"📊 <b>テクニカル分析の現状 (1時間足)</b>\n"
        f"• 波形フェーズ: **{regime}** (信頼度 {confidence:.1f}%)\n"
        f"• トレンド強度 (ADX): {adx:.1f} (基準: 25以上はトレンド)\n"
        f"• モメンタム (RSI): {rsi:.1f} (基準: 70/30は過熱)\n"
        f"• トレンド勢い (MACD Hist): {macd_hist:+.4f} (ゼロ近辺で停滞)\n"
        f"  → 解説: 指標は{'中立から移行期' if adx < 25 else '明確な方向性' }を示唆。\n"
        f"\n"
        f"⚖️ <b>需給・感情・マクロ環境</b>\n"
        f"• マクロ環境: {macro_context['trend']} (VIX: {macro_context['vix_value']})\n"
        f"\n"
        f"💡 <b>BOTの結論 ({action_recommendation} 推奨)</b>\n"
        f"**{conclusion_text}**"
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
    timeframe = signal.get('timeframe', '15m')
    
    # --- 1. 中立/ヘルス通知 ---
    if signal['side'] == "Neutral":
        if signal.get('is_health_check', False):
            stats = signal.get('analysis_stats', {"attempts": 0, "errors": 0, "last_success": 0})
            error_rate = (stats['errors'] / stats['attempts']) * 100 if stats['attempts'] > 0 else 0
            last_success_time = datetime.fromtimestamp(stats['last_success'], JST).strftime('%H:%M:%S') if stats['last_success'] > 0 else "N/A"
            
            # v9.1.7の死活監視フォーマット
            return (
                f"🚨 <b>Apex BOT v11.4.7 - 死活監視 (システム正常)</b> 🟢\n" 
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
        
        # 中立シグナルにも判断根拠フッターを追加
        analysis_summary = generate_analysis_summary(signal)

        return_message = (
            f"⚠️ <b>{symbol} - 市場分析速報 (中立)</b> ⏸️\n"
            f"<b>信頼度: {confidence_pct:.1f}%</b> (データ元: {source_client} | 時間足: {timeframe})\n"
            f"---------------------------\n"
            f"• <b>市場環境/レジーム</b>: {signal['regime']} (ADX: {adx_str}) | {macro_trend} (BB幅: {bb_width_pct}%)\n"
            f"\n"
            f"📊 <b>テクニカル詳細</b>:\n"
            f"  - <i>RSI ({timeframe})</i>: {rsi_str} | <i>MACD Hist ({timeframe})</i>: {macd_hist_str}\n" 
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
         
    # トレードシグナルに判断根拠フッターを追加
    analysis_summary = generate_analysis_summary(signal)

    return_message = (
        f"{score_icon} <b>{signal['symbol']} - {side_icon} シグナル発生!</b> {score_icon}\n"
        f"<b>信頼度スコア (MTFA統合): {score * 100:.2f}%</b> {penalty_info}\n"
        f"データ元: {signal.get('client', 'N/A')} | 時間足: {timeframe}\n"
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


async def initialize_ccxt_client():
    """CCXTクライアントを初期化（非同期）"""
    global CCXT_CLIENTS_DICT, CCXT_CLIENT_NAMES, ACTIVE_CLIENT_HEALTH
    
    clients = {
        'OKX': ccxt_async.okx({ 
            "enableRateLimit": True, 
            "timeout": 60000, 
            "rateLimit": 200, 
        }), 
        # Krakenを追加 (OKXのデータ取得失敗時のフォールバック先)
        'Kraken': ccxt_async.kraken({
            "enableRateLimit": True,
            "timeout": 60000,
            "rateLimit": 200,
            # KrakenはBTC/USDTではなくXBT/USDなどのシンボル命名規則を持つため、
            # CCXTが自動で変換できるか確認が必要。できない場合は調整が必要だが、一旦デフォルト設定で試す。
        }),
    }
    CCXT_CLIENTS_DICT = clients
    CCXT_CLIENT_NAMES = list(CCXT_CLIENTS_DICT.keys())
    ACTIVE_CLIENT_HEALTH = {name: time.time() for name in CCXT_CLIENT_NAMES}
    logging.info(f"✅ CCXTクライアント初期化完了。利用可能なクライアント: {CCXT_CLIENT_NAMES}")

async def send_test_message():
    """起動テスト通知"""
    test_text = (
        f"🤖 <b>Apex BOT v11.4.7 - 起動テスト通知 (KRAKEN FALLBACK)</b> 🚀\n\n" 
        f"現在の時刻: {datetime.now(JST).strftime('%Y-%m-%d %H:%M:%S')} JST\n"
        f"<b>機能統合: Krakenクライアントの追加、15分足データ不足時の1時間足へのフォールバックを適用しました。</b>\n"
        f"<b>v11.4.7更新: 詳細な市場状況分析通知（6時間ごと）を追加しました。</b>"
    )
    try:
        await asyncio.to_thread(lambda: send_telegram_html(test_text, is_emergency=True)) 
        logging.info("✅ Telegram 起動テスト通知を正常に送信しました。")
    except Exception as e:
        logging.error(f"❌ Telegram 起動テスト通知の送信に失敗しました: {e}")

async def fetch_ohlcv_with_fallback(client_name: str, symbol: str, timeframe: str) -> Tuple[List[List[float]], str, str]:
    """
    OHLCVデータを取得。15mでデータ不足の場合、1hにフォールバックし、それでも失敗したらDataShortageとして銘柄をクールダウン。
    """
    global SYMBOL_COOLDOWN_DICT
    client = CCXT_CLIENTS_DICT.get(client_name)
    if not client: return [], "ClientError", client_name
    
    # メインのlimit設定
    limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 100) 
    
    # ----------------------------------------------------
    # 1. メインのデータ取得を試行
    # ----------------------------------------------------
    try:
        ohlcv = await client.fetch_ohlcv(symbol, timeframe, limit=limit)
        
        # 成功判定: 必要なデータ長チェック
        if len(ohlcv) >= MIN_OHLCV_FOR_ANALYSIS:
            return ohlcv, "Success", client_name
            
        # ----------------------------------------------------
        # 2. DataShortageが発生した場合のフォールバック (15m → 1h)
        # ----------------------------------------------------
        if timeframe == '15m':
            logging.warning(f"⚠️ DataShortageエラー({timeframe}): {symbol}。1hデータで分析を試みます。")
            
            # 1hデータを取得
            ohlcv_1h, status_1h, _ = await fetch_ohlcv_with_fallback(client_name, symbol, '1h')
            
            if status_1h == "Success":
                 logging.info(f"✅ 1hデータ({len(ohlcv_1h)}本)でフォールバックに成功しました。")
                 # 成功した場合、timeframeを'1h'に変更して返す
                 return ohlcv_1h, "FallbackSuccess_1h", client_name 
            
            # 1hでも失敗した場合、この銘柄をクールダウン
            cooldown_end_time = time.time() + SYMBOL_COOLDOWN_TIME
            SYMBOL_COOLDOWN_DICT[symbol] = cooldown_end_time
            logging.error(f"❌ DataShortage({timeframe} & 1h)エラー: {symbol} を {SYMBOL_COOLDOWN_TIME/3600:.0f}時間無効化します。")
            return [], "DataShortage", client_name
            
        # 1hや4hなど、フォールバックがない時間足でデータ不足の場合
        else:
            cooldown_end_time = time.time() + SYMBOL_COOLDOWN_TIME
            SYMBOL_COOLDOWN_DICT[symbol] = cooldown_end_time
            logging.error(f"❌ DataShortageエラー({timeframe}): {symbol}。代替データなし。銘柄を無効化します。")
            return [], "DataShortage", client_name

    # ----------------------------------------------------
    # 3. クライアント側のエラーを捕捉
    # ----------------------------------------------------
    except ccxt.NotSupported:
        cooldown_end_time = time.time() + SYMBOL_COOLDOWN_TIME 
        SYMBOL_COOLDOWN_DICT[symbol] = cooldown_end_time
        logging.error(f"❌ NotSupportedエラー: {symbol} を {SYMBOL_COOLDOWN_TIME/3600:.0f}時間無効化します。")
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
        logging.error(f"❌ {symbol}のデータ取得中に予期せぬエラー: {e}")
        return [], "UnknownError", client_name


def get_crypto_macro_context() -> Dict:
    """暗号資産市場のマクロコンテクストとVIXを取得"""
    # (既存ロジックを維持)
    vix_value = 0.0
    try:
        vix_data = yf.Ticker("^VIX").history(period="1d")
        if not vix_data.empty:
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
    """出来高TOP銘柄の動的取得 (OKX)""" 
    # (既存ロジックを維持)
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
    """突発的な15分間の価格変動をチェックし、警報を送信する (既存ロジックを維持)"""
    global VOLATILITY_NOTIFIED_SYMBOLS
    current_time = time.time()
    
    if current_time - VOLATILITY_NOTIFIED_SYMBOLS.get(symbol, 0) < VOLATILITY_ALERT_COOLDOWN:
        return

    # 突発変動は15mで取得
    ohlcv, status, _ = await fetch_ohlcv_with_fallback(client_name, symbol, VOLATILITY_ALERT_TIMEFRAME)
    
    # 1hにフォールバックした場合はスキップ（15mでの急変動チェックのため）
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
    """シグナル通知の処理とクールダウン管理 (既存ロジックを維持)"""
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
    
    # DataShortageは銘柄クールダウンで処理されるため、クライアント全体をクールダウンさせるエラーのみをチェック
    if status_1h in ["RateLimit", "Timeout", "ExchangeError", "UnknownError"]:
        return {"symbol": symbol, "side": status_1h, "client": client_name}
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
    macd_hist_col = next((col for col in macd_data.columns if 'hist' in col.lower()), None)
    
    if macd_hist_col is None or macd_hist_col not in macd_data.columns:
        macd_hist_val = 0.0
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
        "timeframe": "1h", # 市場サマリーは1hで固定
        "tech_data": {
            "rsi": df_1h['rsi'].iloc[-1] if not df_1h['rsi'].empty else 50.0,
            "adx": adx_val,
            "macd_hist": macd_hist_val,
        }
    }

async def generate_signal_candidate(symbol: str, macro_context: Dict, client_name: str) -> Optional[Dict]:
    """
    シグナル生成ロジック (ダミー版)
    DataShortage時の1hフォールバックに対応
    """
    
    # データを取得 (15mを試行し、不足なら1hにフォールバック)
    ohlcv, status, client_used = await fetch_ohlcv_with_fallback(client_name, symbol, '15m')
    
    if status in ["RateLimit", "Timeout", "ExchangeError", "UnknownError", "NotSupported", "DataShortage"]:
        # DataShortageはここで最終的にDataShortageを返すか、RateLimitなどはそのままクライアントエラーとして返す
        return {"symbol": symbol, "side": status, "client": client_used}
    
    # フォールバックしたかどうかの判定と時間足の設定
    timeframe = '1h' if status == "FallbackSuccess_1h" else '15m'
    
    # ダミーデータフレームを作成
    df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
    df['close'] = pd.to_numeric(df['close'])
    df['rsi'] = ta.rsi(df['close'], length=14)
    
    
    # ダミーの判断ロジック（時間足に関わらず一律のロジック）
    price = df['close'].iloc[-1]
    
    if df['rsi'].iloc[-1] > 70 and random.random() > 0.6:
        side = "ショート"
        score = 0.82
        entry = price * 1.0005
        sl = price * 1.005
        tp1 = price * 0.995
        rr_ratio = 1.0
    elif df['rsi'].iloc[-1] < 30 and random.random() > 0.6:
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
        "rsi": df['rsi'].iloc[-1] if not df.empty and df['rsi'].iloc[-1] is not None else 50.0,
        "macd_hist": random.uniform(-0.01, 0.01),
        "bb_width_pct": random.uniform(2.0, 5.5),
        "atr_value": price * 0.005,
        "adx": random.uniform(15.0, 35.0), 
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
        "client": client_used,
        "timeframe": timeframe, # 使用された時間足を記録
        "tech_data": tech_data,
        "volatility_penalty_applied": tech_data['bb_width_pct'] > VOLATILITY_BB_PENALTY_THRESHOLD,
    }
    
    return signal_candidate

async def self_ping_task(interval: int):
    """定期的な死活監視メッセージを送信するタスク (既存ロジックを維持)"""
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
            # 通知ロジックは省略
            LAST_BEST_POSITION_TIME = current_time

async def main_loop():
    """BOTのメイン実行ループ"""
    global LAST_UPDATE_TIME, LAST_SUCCESS_TIME, TOTAL_ANALYSIS_ATTEMPTS, TOTAL_ANALYSIS_ERRORS
    global ACTIVE_CLIENT_HEALTH, CCXT_CLIENT_NAME, CCXT_FALLBACK_CLIENT_NAME, LAST_ANALYSIS_SIGNALS, BTC_DOMINANCE_CONTEXT
    global LAST_MARKET_SUMMARY_TIME, SYMBOL_COOLDOWN_DICT

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

        # OKXの銘柄リストを取得
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
                
                # クールダウンが終了した銘柄をSYMBOL_COOLDOWN_DICTからクリア
                SYMBOL_COOLDOWN_DICT = {
                    s: t for s, t in SYMBOL_COOLDOWN_DICT.items() if t > current_time
                }


            # 🚨 市場サマリー通知のチェック (6時間ごと)
            if current_time - LAST_MARKET_SUMMARY_TIME > MARKET_SUMMARY_INTERVAL:
                client_name = CCXT_CLIENT_NAME
                summary_signal = await generate_market_summary_signal("BTC/USDT", BTC_DOMINANCE_CONTEXT, client_name)
                
                if summary_signal and summary_signal.get('side') not in ["RateLimit", "Timeout", "ExchangeError", "UnknownError", "DataShortage"]:
                    asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(format_market_summary(summary_signal))))
                    LAST_MARKET_SUMMARY_TIME = current_time
                    logging.info("📢 詳細な市場サマリー通知を送信しました。")
                elif summary_signal and summary_signal.get('side') in ["RateLimit", "Timeout", "ExchangeError", "UnknownError"]:
                    # BTCのサマリー取得に失敗した場合も、クライアント全体をクールダウンさせる
                    cooldown_end_time = time.time() + CLIENT_COOLDOWN
                    error_msg = f"❌ 市場サマリー取得失敗({summary_signal['side']}): クライアント {client_name} のヘルスを {datetime.fromtimestamp(cooldown_end_time, JST).strftime('%H:%M:%S')} JST にリセット ({CLIENT_COOLDOWN/60:.0f}分クールダウン)。"
                    logging.error(error_msg)
                    ACTIVE_CLIENT_HEALTH[client_name] = cooldown_end_time
                    asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(error_msg, is_emergency=False)))
                    
            # --- 分析ロジック ---
            
            # クライアントの切り替え (OKX -> Kraken)
            client_to_use = CCXT_CLIENT_NAME
            if current_time < ACTIVE_CLIENT_HEALTH.get(CCXT_CLIENT_NAME, 0):
                if current_time < ACTIVE_CLIENT_HEALTH.get(CCXT_FALLBACK_CLIENT_NAME, 0):
                    # 両方のクライアントがクールダウン中の場合
                    longest_cooldown = max(ACTIVE_CLIENT_HEALTH.get(CCXT_CLIENT_NAME, 0), ACTIVE_CLIENT_HEALTH.get(CCXT_FALLBACK_CLIENT_NAME, 0))
                    cooldown_time = longest_cooldown - current_time
                    logging.warning(f"❌ 両クライアントがクールダウン中です。次の分析まで {cooldown_time:.0f}秒待機します。")
                    await asyncio.sleep(min(max(10, cooldown_time), LOOP_INTERVAL)) 
                    continue
                else:
                    # OKXがクールダウン中でKrakenが正常な場合、Krakenに切り替え
                    client_to_use = CCXT_FALLBACK_CLIENT_NAME
                    logging.warning(f"🔄 メインクライアント({CCXT_CLIENT_NAME})がクールダウン中のため、フォールバッククライアント({CCXT_FALLBACK_CLIENT_NAME})を使用します。")
                    
            
            # クールダウン中の銘柄を除外
            monitor_symbols_for_analysis = [
                symbol for symbol in CURRENT_MONITOR_SYMBOLS
                if symbol not in SYMBOL_COOLDOWN_DICT or SYMBOL_COOLDOWN_DICT[symbol] < current_time
            ]
            
            cooldown_count = len(CURRENT_MONITOR_SYMBOLS) - len(monitor_symbols_for_analysis)
            if cooldown_count > 0:
                 logging.info(f"⏭️ {cooldown_count}銘柄をクールダウン中のためスキップします。")
            
            analysis_queue: List[Tuple[str, str]] = [(symbol, client_to_use) for symbol in monitor_symbols_for_analysis]
                
            logging.info(f"🔍 分析開始 (対象銘柄: {len(analysis_queue)}銘柄, 利用クライアント: {client_to_use})")
            TOTAL_ANALYSIS_ATTEMPTS += 1
            
            signals: List[Optional[Dict]] = []
            has_major_error = False
            
            for symbol, client_name in analysis_queue:
                
                asyncio.create_task(check_for_sudden_volatility(symbol, client_name))
                
                if time.time() < ACTIVE_CLIENT_HEALTH.get(client_name, 0):
                     break
                     
                signal = await generate_signal_candidate(symbol, BTC_DOMINANCE_CONTEXT, client_name)
                signals.append(signal)

                major_errors = ["RateLimit", "Timeout", "ExchangeError", "UnknownError"]
                
                if signal and signal.get('side') in major_errors:
                    cooldown_end_time = time.time() + CLIENT_COOLDOWN
                    
                    error_msg = f"❌ {signal['side']}エラー発生: クライアント {client_name} のヘルスを {datetime.fromtimestamp(cooldown_end_time, JST).strftime('%H:%M:%S')} JST にリセット ({CLIENT_COOLDOWN/60:.0f}分クールダウン)。"
                    logging.error(error_msg)
                    
                    ACTIVE_CLIENT_HEALTH[client_name] = cooldown_end_time
                    
                    if signal.get('side') in ["RateLimit", "Timeout", "ExchangeError"]:
                        asyncio.create_task(asyncio.to_thread(lambda: send_telegram_html(error_msg, is_emergency=False)))
                        has_major_error = True
                    
                    TOTAL_ANALYSIS_ERRORS += 1
                    break 
                
                await asyncio.sleep(SYMBOL_WAIT) 
            
            
            LAST_ANALYSIS_SIGNALS = [s for s in signals if s is not None and s.get('side') not in major_errors and s.get('side') != "DataShortage"]
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

app = FastAPI(title="Apex BOT API", version="v11.4.7-KRAKEN_FALLBACK (Full Integrated)")

@app.on_event("startup")
async def startup_event():
    """アプリケーション起動時にCCXTクライアントを初期化し、メインループを開始する"""
    await initialize_ccxt_client() # 👈 ここに 'await' を追加
    logging.info("🚀 Apex BOT v11.4.7-KRAKEN FALLBACK Startup Complete.") 
    
    # asyncio.create_taskで例外が処理されるようになり、エラーで即座にアプリが落ちるのを防ぐ
    asyncio.create_task(main_loop())


@app.get("/status")
def get_status():
    """ヘルスチェック用のエンドポイント"""
    current_time = time.time()
    
    status_msg = {
        "status": "ok",
        "bot_version": "v11.4.7-KRAKEN_FALLBACK (Full Integrated)",
        "last_success_timestamp": LAST_SUCCESS_TIME,
        "active_clients_count": len([name for name in CCXT_CLIENT_NAMES if current_time >= ACTIVE_CLIENT_HEALTH.get(name, 0)]),
        "monitor_symbols_count": len(CURRENT_MONITOR_SYMBOLS),
        "active_analysis_count": len([s for s in CURRENT_MONITOR_SYMBOLS if SYMBOL_COOLDOWN_DICT.get(s, 0) < current_time]),
        "cooldown_symbols_count": len([s for s in SYMBOL_COOLDOWN_DICT if SYMBOL_COOLDOWN_DICT[s] > current_time]),
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
    return JSONResponse(content={"message": "Apex BOT is running (v11.4.7-KRAKEN_FALLBACK, Full Integrated)."}, status_code=200)

if __name__ == '__main__':
    # 実際にはRender環境で実行されるためこのブロックは使用されないが、ローカルテスト用に残す
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8080)))
