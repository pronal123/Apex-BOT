# ====================================================================================
# Apex BOT v19.0.18 - FX-Macro-Sensitivity Patch (完全版 - 表示ロジック＆デプロイ修正)
#
# 修正ポイント:
# 1. 【表示ロジック復元】format_integrated_analysis_message関数内で、v19.0.17に存在した
#    プラス/マイナス要因の表示ロジックを完全に復元しました。
# 2. 【デプロイ修正】startup_event関数内で、同期関数send_position_status_notification()
#    への不要な 'await' 呼び出しを削除し、デプロイ時のTypeErrorを解消しました。
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

# ロギング設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# .envファイルから環境変数を読み込む
load_dotenv()

# ====================================================================================
# CONFIG & CONSTANTS
# ====================================================================================

JST = timezone(timedelta(hours=9))

# 出来高TOP30に加えて、主要な基軸通貨をDefaultに含めておく (現物シンボル形式 BTC/USDT)
DEFAULT_SYMBOLS = [
    "BTC/USDT", "ETH/USDT", "SOL/USDT", "ADA/USDT", "XRP/USDT", "DOT/USDT",
    "DOGE/USDT", "AVAX/USDT", "LINK/USDT", "LTC/USDT", "MATIC/USDT", "TRX/USDT",
    "ATOM/USDT", "NEAR/USDT", "ALGO/USDT", "XLM/USDT", "BCH/USDT", "ETC/USDT",
    "UNI/USDT", "ICP/USDT", "FIL/USDT", "AAVE/USDT", "AXS/USDT", "SAND/USDT",
    "GALA/USDT", "FTM/USDT", "HBAR/USDT", "VET/USDT", "GRT/USDT", "SHIB/USDT"
]
TOP_SYMBOL_LIMIT = 30      # 出来高上位30銘柄を監視
LOOP_INTERVAL = 180        # メインループの実行間隔（秒）
REQUEST_DELAY_PER_SYMBOL = 0.5 # 銘柄ごとのAPIリクエストの遅延（秒）

TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.70             # シグナルを通知する最低スコア
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} # 取得するOHLCVの足数

# 💡 v19.0.18 定数
RSI_DIVERGENCE_PENALTY = 0.12        
ADX_TREND_STRENGTH_BONUS = 0.06      
ADX_ABSENCE_THRESHOLD = 20           # ADX不在判定閾値
ADX_TREND_ABSENCE_PENALTY = 0.07     
RSI_SUPPORT_TOUCH_BONUS = 0.05       
MULTI_PIVOT_CONFLUENCE_BONUS = 0.04  
VPVR_GAP_PENALTY = 0.08              

DXY_SURGE_PENALTY = 0.15             
USDJPY_RISK_OFF_PENALTY = 0.10       
FX_MACRO_STABILITY_BONUS = 0.07      

LONG_TERM_REVERSAL_PENALTY = 0.25   
MACD_DIVERGENCE_PENALTY = 0.18       # MACDダイバージェンス
MACD_CROSS_PENALTY = 0.18            # MACDクロス無しペナルティ
MACD_CROSS_BONUS = 0.07             

VWAP_BONUS_POINT = 0.05             
BB_SQUEEZE_BONUS = 0.04             
BB_WIDE_PENALTY = 0.05               
MIN_RRR_THRESHOLD = 3.0              
RRR_BONUS_MULTIPLIER = 0.04          

LIQUIDITY_BONUS_POINT = 0.06        
WHALE_IMBALANCE_PENALTY = 0.12      
WHALE_IMBALANCE_BONUS = 0.08        
WHALE_IMBALANCE_THRESHOLD = 0.60    

FGI_PROXY_BONUS_MAX = 0.10          
FGI_PROXY_PENALTY_MAX = 0.10        

RSI_OVERBOUGHT_PENALTY = 0.10        
RSI_OVERBOUGHT_THRESHOLD = 60        
RSI_MOMENTUM_LOW = 40                
BASE_SCORE = 0.40                   
RANGE_TRAIL_MULTIPLIER = 3.0        
VOLUME_CONFIRMATION_MULTIPLIER = 2.5 
DTS_RRR_DISPLAY = 5.0                # 利確目標TP1に使うRRR倍率

TRADE_EXECUTION_THRESHOLD = 0.85    
MAX_RISK_PER_TRADE_USDT = 5.0       
MAX_RISK_CAPITAL_PERCENT = 0.01     
MIN_USDT_BALANCE_TO_TRADE = 50.0    

# ====================================================================================
# GLOBAL STATE
# ====================================================================================

CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS
LAST_ANALYSIS_SIGNALS: Dict[str, Any] = {}
LAST_SUCCESS_TIME: float = 0
TRADE_COOLDOWN_TIMESTAMPS: Dict[str, float] = {}
ACTUAL_POSITIONS: Dict[str, Any] = {}
ORDER_BOOK_CACHE: Dict[str, Any] = {}
CURRENT_USDT_BALANCE: float = 0.0
FX_MACRO_CONTEXT: Dict = {} 

EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
CCXT_CLIENT_NAME: str = os.environ.get('CCXT_EXCHANGE', 'binance')
CCXT_API_KEY: str = os.environ.get('CCXT_API_KEY', 'YOUR_API_KEY')
CCXT_SECRET: str = os.environ.get('CCXT_SECRET', 'YOUR_SECRET')

# ====================================================================================
# UTILITIES & FORMATTING
# ====================================================================================

def get_tp_reach_time(timeframe: str) -> str:
    """利確までの時間目安を計算する"""
    if timeframe == '15m': return "数時間以内 (1-5本程度の15m足)"
    if timeframe == '1h': return "24時間以内 (1-5本程度の1h足)"
    if timeframe == '4h': return "数日以内 (1-3本程度の4h足)"
    return "不明"

def format_price_utility(price: float) -> str:
    """価格を適切な桁数でフォーマットする"""
    if price >= 100: return f"{price:,.2f}"
    if price >= 10: return f"{price:,.3f}"
    if price >= 1: return f"{price:,.4f}"
    return f"{price:,.6f}"

def format_usdt(amount: float) -> str:
    """USDT残高をフォーマットする"""
    return f"{amount:,.2f}"

def get_estimated_win_rate(score: float, timeframe: str) -> float:
    """スコアと時間足に基づき推定勝率を計算する"""
    base_rate = score * 0.50 + 0.35 

    if timeframe == '15m':
        return max(0.40, min(0.75, base_rate - 0.05))
    if timeframe == '1h':
        return max(0.45, min(0.85, base_rate))
    if timeframe == '4h':
        return max(0.50, min(0.90, base_rate + 0.05))
    
    return base_rate

def send_telegram_html(message: str):
    """HTML形式でTelegramにメッセージを送信する (同期的に実装)"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        logging.warning("Telegram設定がされていません。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }
    
    try:
        response = requests.post(url, data=payload)
        response.raise_for_status() 
    except requests.exceptions.RequestException as e:
        logging.error(f"Telegram送信エラー: {e}")

def send_position_status_notification(message: str):
    """ポジションの状況変化をTelegramに通知する (ここではシグナル通知として代用)"""
    # send_telegram_htmlは同期関数なので、ここではawaitは不要
    send_telegram_html(message)

def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """分析結果を統合したTelegramメッセージをHTML形式で作成する (v19.0.18修正版)"""

    valid_signals = [s for s in signals if s.get('side') == 'ロング']
    if not valid_signals:
        return ""

    high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
    if not high_score_signals:
        return ""

    best_signal = max(
        high_score_signals,
        key=lambda s: (s.get('score', 0.5), s.get('rr_ratio', 0.0))
    )

    # データ抽出
    timeframe = best_signal.get('timeframe', '1h')
    price = best_signal.get('entry_price', 0.0)
    score_raw = best_signal.get('score', 0.0)
    entry_price = best_signal.get('entry_price', 0.0)
    trade_plan_data = best_signal.get('trade_plan', {})
    sl_price = trade_plan_data.get('sl_price', 0.0)
    tp1_price = trade_plan_data.get('tp1_price', 0.0)
    rr_ratio = best_signal.get('rr_ratio', 0.0)
    risk_amount = best_signal.get('risk_amount_usdt', 0.0)
    reward_amount = risk_amount * rr_ratio if risk_amount else 0.0
    
    is_tradable = score_raw >= TRADE_EXECUTION_THRESHOLD
    
    display_symbol = symbol
    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100

    if score_raw >= 0.90:
        confidence_text = "<b>極めて高い (確信度:S)</b>"
    elif score_raw >= 0.85: 
        confidence_text = "<b>極めて高い (確信度:A)</b>"
    elif score_raw >= 0.75:
        confidence_text = "<b>高い (確信度:B)</b>"
    else:
        confidence_text = "中程度 (確信度:C)"

    direction_emoji = "🚀"
    direction_text = "<b>ロング (現物買い推奨)</b>"

    header = (
        f"{direction_emoji} <b>【APEX BOT SIGNAL: {confidence_text}】</b> {direction_emoji}\n\n"
        f"<b>🥇 BOT自動取引実行シグナル</b>\n"
        f"銘柄: **{display_symbol}**\n"
        f"方向: {direction_text}\n"
        f"現在価格: <code>${format_price_utility(price)}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    trade_plan = (
        f"### 🎯 <b>トレードプラン概要 ({timeframe})</b>\n"
        f"  - <b>エントリー価格</b>: <code>${format_price_utility(entry_price)}</code>\n"
        f"  - <b>損切り (SL)</b>: <code>${format_price_utility(sl_price)}</code>\n"
        f"    (リスク許容額: ${format_usdt(risk_amount)})\n"
        f"  - <b>利確目標 (TP1)</b>: <code>${format_price_utility(tp1_price)}</code>\n"
        f"    (リワード額: ${format_usdt(reward_amount)})\n"
        f"  - <b>リスクリワード比率</b>: <b>1:{rr_ratio:.2f}</b>\n"
        f"  - <b>推定レバレッジ</b>: 10x (想定)\n\n"
    )

    tech_data = best_signal.get('tech_data', {})
    regime = "強い上昇トレンド" if tech_data.get('adx', 0.0) >= 30 and tech_data.get('di_plus', 0.0) > tech_data.get('di_minus', 0.0) else ("トレンド相場" if tech_data.get('adx', 0.0) >= ADX_ABSENCE_THRESHOLD else "レンジ相場")
    fgi_score = tech_data.get('sentiment_fgi_proxy_value', 0.0) 
    fgi_sentiment = "リスクオン" if fgi_score > 0 else ("リスクオフ" if fgi_score < 0 else "中立")

    fx_bonus = tech_data.get('fx_macro_stability_bonus', 0.0)
    fx_dxy_penalty = tech_data.get('dxy_surge_penalty', 0.0)
    fx_usdjpy_penalty = tech_data.get('usdjpy_risk_off_penalty', 0.0)
    fx_status = "安定"
    if fx_bonus > 0: fx_status = "ポジティブ"
    if fx_dxy_penalty < 0 or fx_usdjpy_penalty < 0: fx_status = "リスク警戒"


    summary = (
        f"<b>💡 分析サマリー</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>分析スコア</b>: <code>{score_100:.2f} / 100</code> (信頼度: {confidence_text})\n"
        f"  - <b>予測勝率</b>: <code>約 {win_rate:.1f}%</code>\n"
        f"  - <b>時間軸 (メイン)</b>: <code>{timeframe}</code>\n"
        f"  - <b>決済までの目安</b>: {get_tp_reach_time(timeframe)}\n"
        f"  - <b>市場の状況</b>: {regime} (ADX: {tech_data.get('adx', 0.0):.1f})\n"
        f"  - <b>恐怖指数 (FGI) プロキシ</b>: {fgi_sentiment} ({abs(fgi_score*100):.1f}点影響)\n"
        f"  - <b>為替 (FX) マクロ</b>: {fx_status}\n\n"
    )

    plus_factors = []
    minus_factors = []
    
    # --- プラス要因の評価 ---
    lt_penalty = tech_data.get('long_term_reversal_penalty', False)
    lt_trend_str = tech_data.get('long_term_trend', 'N/A')
    
    if not lt_penalty:
        plus_factors.append(f"長期 ({lt_trend_str}) トレンドとの一致") 
        
    if tech_data.get('macd_cross_valid', False):
        plus_factors.append(f"MACDゴールデンクロス確証 (+{MACD_CROSS_BONUS*100:.1f}点)")

    # 復元: 構造的ピボットボーナス
    if tech_data.get('structural_pivot_bonus', 0.0) > 0:
        plus_factors.append(f"重要支持線からの反発確証 ({tech_data.get('fib_proximity_level', 'N/A')}確認)")
        
    if tech_data.get('multi_pivot_confluence_bonus_value', 0.0) > 0:
        plus_factors.append(f"🧱 **多重ピボット**コンフルエンス (+{MULTI_PIVOT_CONFLUENCE_BONUS*100:.1f}点)")
        
    # 復元: RSIサポートタッチボーナス
    if tech_data.get('rsi_support_touch_bonus', 0.0) > 0:
        plus_factors.append(f"📈 RSIサポート(40以下)からの反発 (+{RSI_SUPPORT_TOUCH_BONUS*100:.1f}点)")
        
    if tech_data.get('adx_strength_bonus_value', 0.0) > 0:
        plus_factors.append(f"💪 **ADXトレンド強度**の裏付け (+{ADX_TREND_STRENGTH_BONUS*100:.1f}点)")
        
    if tech_data.get('volume_confirmation_bonus', 0.0) > 0:
        plus_factors.append(f"出来高による裏付け (+{tech_data.get('volume_confirmation_bonus', 0.0)*100:.1f}点)")
        
    if tech_data.get('liquidity_bonus_value', 0.0) > 0:
        plus_factors.append(f"板の厚み (流動性) 優位 (+{tech_data.get('liquidity_bonus_value', 0.0)*100:.1f}点)")
        
    if tech_data.get('vwap_confirm_ok', False):
        plus_factors.append(f"価格がVWAPよりも上 (買い圧力優位)")

    # 復元: BBスクイーズ
    if tech_data.get('bb_squeeze_ok', False):
        plus_factors.append(f"ボラティリティ圧縮後のブレイク期待 (+{BB_SQUEEZE_BONUS*100:.1f}点)")

    # 復元: RRRボーナス
    if tech_data.get('rrr_bonus_value', 0.0) > 0:
        plus_factors.append(f"優秀なRRR ({rr_ratio:.2f}+) (+{tech_data.get('rrr_bonus_value', 0.0)*100:.1f}点)")

    if tech_data.get('whale_imbalance_bonus', 0.0) > 0:
        plus_factors.append(f"🐋 **クジラ**の買い圧力優位 (+{WHALE_IMBALANCE_BONUS*100:.1f}点)")

    if fx_bonus > 0:
        plus_factors.append(f"💸 **FX市場**の安定 (+{FX_MACRO_STABILITY_BONUS*100:.1f}点)")

    if fgi_score > 0:
        plus_factors.append(f"😨 FGIプロキシがリスクオンを示唆 (+{fgi_score*100:.1f}点)")


    # --- マイナス要因の評価 ---

    if lt_penalty:
        minus_factors.append(f"長期トレンド ({lt_trend_str}) と逆行 (-{LONG_TERM_REVERSAL_PENALTY*100:.1f}点)")
    
    if tech_data.get('macd_divergence_penalty', False):
        minus_factors.append(f"MACDダイバージェンスの兆候 (トレンド終焉リスク) (-{MACD_DIVERGENCE_PENALTY*100:.1f}点)")

    if tech_data.get('rsi_divergence_penalty', False):
        minus_factors.append(f"⚠️ **RSI弱気ダイバージェンス** (トレンド終焉リスク) (-{RSI_DIVERGENCE_PENALTY*100:.1f}点)")

    if tech_data.get('stoch_filter_penalty', 0) > 0:
        minus_factors.append(f"ストキャスティクス過熱 (買われすぎ80以上) (-{0.15*100:.1f}点)")

    # 復元: RSI過熱ペナルティ
    if tech_data.get('rsi_overbought_penalty', 0) > 0:
        minus_factors.append(f"RSI過熱 (RSI 60以上) (-{RSI_OVERBOUGHT_PENALTY*100:.1f}点)") 
        
    if tech_data.get('adx_absence_penalty', 0.0) < 0:
        minus_factors.append(f"📉 **ADXトレンド不在** (ADX < {ADX_ABSENCE_THRESHOLD} / レンジリスク) (-{ADX_TREND_ABSENCE_PENALTY*100:.1f}点)")

    if tech_data.get('vpvr_gap_penalty', 0.0) < 0:
        minus_factors.append(f"🚧 **出来高空隙** (SLまでの急落リスク) (-{VPVR_GAP_PENALTY*100:.1f}点)")
    
    # 復元: BB幅過大ペナルティ
    if tech_data.get('bb_wide_penalty', False):
        minus_factors.append(f"ボラティリティ過大 (BB幅 > 8.0%) (-{BB_WIDE_PENALTY*100:.1f}点)")

    if tech_data.get('whale_imbalance_penalty', 0.0) < 0:
         minus_factors.append(f"🐋 **クジラ**の売り圧力優位 (-{WHALE_IMBALANCE_PENALTY*100:.1f}点)")

    if fx_dxy_penalty < 0:
        minus_factors.append(f"⚠️ **DXY急騰** (グローバルリスクオフ) (-{DXY_SURGE_PENALTY*100:.1f}点)")

    if fx_usdjpy_penalty < 0:
        minus_factors.append(f"🚨 **USD/JPY急激な円高** (資金逃避リスク) (-{USDJPY_RISK_OFF_PENALTY*100:.1f}点)")

    if fgi_score < 0:
        minus_factors.append(f"😨 FGIプロキシがリスクオフを示唆 (-{FGI_PROXY_PENALTY_MAX*100:.1f}点)")


    # プラス要因セクション
    plus_section = (
        f"<b>📊 分析の確証 (高得点要因)</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    for factor in plus_factors:
        plus_section += f"  ✅ {factor}\n"
    if not plus_factors:
        plus_section += f"  <i>(高得点要因は特にありません)</i>\n"
    plus_section += "\n"

    # マイナス要因セクション
    minus_section = (
        f"<b>🚨 懸念/ペナルティ要因 (マイナス)</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
    )
    for factor in minus_factors:
        minus_section += f"  ❌ {factor}\n"
    if not minus_factors:
        minus_section += f"  <i>(目立ったリスク要因はありません)</i>\n"
    minus_section += "\n"

    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ このシグナルは自動売買の{'対象です。' if is_tradable else '対象外です。'}</pre>"
        f"<i>Bot Ver: v19.0.18 (FX-Macro-Sensitivity Patch)</i>"
    )

    return header + trade_plan + summary + plus_section + minus_section + footer


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化する"""
    global EXCHANGE_CLIENT, CCXT_CLIENT_NAME
    
    exchange_class = getattr(ccxt_async, CCXT_CLIENT_NAME)
    EXCHANGE_CLIENT = exchange_class({
        'apiKey': CCXT_API_KEY,
        'secret': CCXT_SECRET,
        'enableRateLimit': True,
        'options': {'defaultType': 'spot'} 
    })
    logging.info(f"CCXTクライアントを {CCXT_CLIENT_NAME} で初期化しました。")

async def fetch_current_balance_usdt(client: ccxt_async.Exchange) -> float:
    """現在のUSDT現物残高を取得する (シミュレーション)"""
    try:
        if not client: return 0.0
        # 実際にはCCXTのfetch_balanceを呼び出す。ここではシミュレーション値を返す
        return 10000.0 
    except Exception as e:
        logging.error(f"USDT残高取得エラー: {e}")
        return 0.0

async def update_symbols_by_volume(client: ccxt_async.Exchange):
    """出来高に基づいて監視銘柄リストを更新する (簡易版)"""
    global CURRENT_MONITOR_SYMBOLS
    CURRENT_MONITOR_SYMBOLS = DEFAULT_SYMBOLS
    logging.info(f"監視銘柄リストを更新しました。合計: {len(CURRENT_MONITOR_SYMBOLS)} 銘柄")
        
async def fetch_ohlcv_with_fallback(exchange_name: str, symbol: str, timeframe: str) -> Tuple[Optional[pd.DataFrame], str, Optional[ccxt.Exchange]]:
    """OHLCVデータを取得し、失敗時にフォールバックする (簡易版)"""
    global EXCHANGE_CLIENT
    
    try:
        if not EXCHANGE_CLIENT: 
            return None, "Error: Client not initialized", None

        # シミュレーションデータを作成 (実際の取引所から取得する部分を置き換え)
        limit = REQUIRED_OHLCV_LIMITS[timeframe]
        
        # 簡易的な価格変動のシミュレーション
        current_time = int(time.time() * 1000)
        time_step = {'15m': 15 * 60, '1h': 60 * 60, '4h': 4 * 60 * 60}[timeframe]
        
        data = []
        base_price = 65000.0 * (1 + random.uniform(-0.01, 0.01)) # ランダムな基準価格
        for i in range(limit):
            t = current_time - (limit - i) * time_step * 1000
            o = base_price * (1 + random.uniform(-0.001, 0.001))
            c = o * (1 + random.uniform(-0.005, 0.005))
            h = max(o, c) * (1 + random.uniform(0, 0.001))
            l = min(o, c) * (1 - random.uniform(0, 0.001))
            v = np.random.uniform(5000, 20000)
            data.append([t, o, h, l, c, v])
            base_price = c # 次の足のベースを現在の終値とする

        df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        
        return df, "Success", EXCHANGE_CLIENT

    except Exception as e:
        return None, f"Error: {e}", EXCHANGE_CLIENT

async def fetch_order_book_depth(symbol: str):
    """オーダーブック（板情報）を取得し、キャッシュに保存する (簡易版)"""
    global EXCHANGE_CLIENT, ORDER_BOOK_CACHE
    if not EXCHANGE_CLIENT: return

    try:
        # シミュレーションデータ
        price = 65000.0
        # 買い圧力が少し強いシミュレーション
        bids = [[price - 50 * i, 15 + 3 * i] for i in range(1, 10)]
        asks = [[price + 50 * i, 10 + 2 * i] for i in range(1, 10)]
        
        orderbook = {'bids': bids, 'asks': asks}
        ORDER_BOOK_CACHE[symbol] = orderbook
        
    except Exception as e:
        ORDER_BOOK_CACHE[symbol] = None

async def get_fx_macro_context() -> Dict:
    """💡 v19.0.18: 為替市場データ（USD/JPY, DXY）を取得し、マクロセンチメントを計算する (簡易シミュレーションを強化)"""
    
    fx_data = {}
    try:
        # yfinanceを使用してUSD/JPYとDXY指数 (^DXY) のデータを取得
        
        # 1. DXY (ドルインデックス)
        dxy_ticker = yf.Ticker("^DXY")
        dxy_hist = dxy_ticker.history(period="30d", interval="1d")
        if not dxy_hist.empty:
            dxy_close = dxy_hist['Close']
            dxy_20_day_avg = dxy_close.iloc[-20:].mean()
            dxy_last = dxy_close.iloc[-1]
            dxy_change = (dxy_last - dxy_close.iloc[-2]) / dxy_close.iloc[-2]

            # DXY急騰チェック: 過去20日の平均を上回り、かつ前日比が0.5%以上 (シミュレーション値)
            if dxy_last > dxy_20_day_avg * 1.005 and dxy_change > 0.005:
                fx_data['dxy_surge_penalty'] = -DXY_SURGE_PENALTY
            elif abs(dxy_change) < 0.001: # DXY安定
                 fx_data['dxy_stability'] = True
            
        # 2. USD/JPY
        usdjpy_ticker = yf.Ticker("USDJPY=X")
        usdjpy_hist = usdjpy_ticker.history(period="30d", interval="1d")
        if not usdjpy_hist.empty:
            usdjpy_close = usdjpy_hist['Close']
            usdjpy_change = (usdjpy_close.iloc[-1] - usdjpy_close.iloc[-2]) / usdjpy_close.iloc[-2] 

            # 急激な円高（USD/JPYの急落: リスクオフの円買い）チェック
            if usdjpy_change < -0.008: 
                fx_data['usdjpy_risk_off_penalty'] = -USDJPY_RISK_OFF_PENALTY
            elif usdjpy_change > 0.001 and fx_data.get('dxy_stability'): 
                 fx_data['usdjpy_stability_bonus'] = FX_MACRO_STABILITY_BONUS


    except Exception as e:
        logging.warning(f"FXデータ (yfinance) 取得エラー: {e}")
        # エラー時は中立と判断
        pass 

    # FGIプロキシ（既存ロジック）
    # BTCのボラティリティを簡易的にシミュレーション
    btc_volatility_proxy = random.uniform(0.1, 0.6) 
    fgi_value = 0.0
    if btc_volatility_proxy > 0.5:
        fgi_value = -FGI_PROXY_PENALTY_MAX
    elif btc_volatility_proxy < 0.2:
        fgi_value = FGI_PROXY_BONUS_MAX

    fx_data['sentiment_fgi_proxy_value'] = fgi_value

    return fx_data


# ====================================================================================
# ANALYSIS CORE (v19.0.18 強化版)
# ====================================================================================

def calculate_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """テクニカル指標を計算する (v19.0.18)"""
    df = df.copy()

    # SMA/EMA
    df.ta.sma(length=50, append=True)
    df.ta.ema(length=200, append=True)

    # MACD
    macd = df.ta.macd(append=True)
    df['MACDh_12_26_9'] = macd['MACDh_12_26_9']

    # RSI
    df.ta.rsi(length=14, append=True)

    # Stochastics
    stoch = df.ta.stoch(append=True)
    df['STOCHk_14_3_3'] = stoch['STOCHk_14_3_3']
    df['STOCHd_14_3_3'] = stoch['STOCHd_14_3_3']

    # BBANDS
    bbands = df.ta.bbands(append=True)
    df['BBL_5_2.0'] = bbands['BBL_5_2.0']
    df['BBU_5_2.0'] = bbands['BBU_5_2.0']

    # VWAP 
    df['VWAP'] = df.ta.vwap(append=False)

    # ATR (AvgRange)
    df['AvgRange'] = df.ta.atr(length=14)

    # ADX/DI 
    adx_data = df.ta.adx(length=14)
    df['ADX'] = adx_data['ADX_14']
    df['DMP'] = adx_data['DMP_14'] # +DI
    df['DMN'] = adx_data['DMN_14'] # -DI
    
    # VPVRプロキシの計算 (出来高プロファイル: 簡易版)
    df['Volume_Avg_50'] = df['volume'].rolling(50).mean()
    # 直近5期間の出来高が過去50期間平均の10%以下であるかをチェックする簡易ロジック
    df['VPVR_Gap_Proxy'] = df.apply(lambda row: row['Volume_Avg_50'] * 0.1 > row['volume'] if row['Volume_Avg_50'] else False, axis=1)

    return df.dropna().reset_index(drop=True)

def calculate_risk_reward_ratio(entry: float, sl: float, tp1: float) -> float:
    """リスクリワード比率を計算する"""
    if sl >= entry: return 0.0 
    risk = entry - sl
    reward = tp1 - entry
    return round(reward / risk, 2) if risk > 0 else 0.0

def get_fibonacci_level(high: float, low: float, level: float) -> float:
    """フィボナッチリトレースメントレベルを計算する"""
    return low + (high - low) * level

def check_rsi_divergence(df: pd.DataFrame, current_price: float, side: str) -> bool:
    """RSIダイバージェンス（弱気）をチェックする (簡易版)"""
    
    if len(df) < 50: return False

    rsi = df['RSI_14'].iloc[-20:]
    close = df['close'].iloc[-20:]

    if side == 'ロング':
        peak_index = close.idxmax()
        if peak_index == close.index[0]: return False 

        prev_close = close.loc[:peak_index - 1]
        if prev_close.empty: return False
        
        prev_peak_index = prev_close.idxmax()
        
        # 強気ダイバージェンス（価格安値切り下げ、RSI安値切り上げ）の逆
        # 弱気ダイバージェンス（価格高値切り上げ、RSI高値切り下げ）のチェック
        price_higher = close.loc[peak_index] > close.loc[prev_peak_index]
        rsi_lower = rsi.loc[peak_index] < rsi.loc[prev_peak_index]
        
        if price_higher and rsi_lower:
            return True 
            
    return False

def get_multi_pivot_confluence(sl_price: float, fib_levels: Dict, current_price: float, side: str) -> float:
    """SL近辺の多重ピボット/Fibコンフルエンスボーナスを計算する"""
    
    confluence_count = 0
    confluence_zone = current_price * 0.001
    
    for level, price in fib_levels.items():
        if abs(sl_price - price) <= confluence_zone:
            confluence_count += 1
            
    pivot_ratios = [0.995, 0.99] 
    
    for ratio in pivot_ratios:
        pivot_price_proxy = current_price * ratio
        if abs(sl_price - pivot_price_proxy) <= confluence_zone:
             confluence_count += 1
             
    if confluence_count >= 2:
        return MULTI_PIVOT_CONFLUENCE_BONUS
        
    return 0.0

def check_vpvr_gap_penalty(df: pd.DataFrame, sl_price: float, side: str) -> bool:
    """エントリー価格とSLの間に出来高空隙（Gap）があるかをチェックする (簡易版)"""
    
    if side == 'ロング':
        # 直近5足で出来高空隙プロキシが確認されたらペナルティ
        return df.iloc[-5:]['VPVR_Gap_Proxy'].any()
            
    return False

def get_liquidity_bonus(symbol: str, price: float, side: str) -> float:
    """オーダーブックのデータに基づき流動性ボーナスを計算する (簡易版)"""
    orderbook = ORDER_BOOK_CACHE.get(symbol)
    if not orderbook or not orderbook.get('bids'):
        return 0.0

    # 簡易的に、買い注文の合計サイズが閾値を超えていればボーナス
    total_buy_amount = sum(item[1] for item in orderbook['bids'])
    if total_buy_amount > 50: 
        return LIQUIDITY_BONUS_POINT
        
    return 0.0

def get_whale_bias_score(symbol: str, price: float, side: str) -> Tuple[float, float]:
    """🐋 オーダーブック内の大口注文の偏り（クジラバイアス）を計算する (簡易版)"""
    orderbook = ORDER_BOOK_CACHE.get(symbol)
    if not orderbook or not orderbook.get('bids') or not orderbook.get('asks'):
        return 0.0, 0.0

    # USDT換算の総出来高 (簡易)
    total_buy_volume_usdt = sum(p * a for p, a in orderbook['bids'])
    total_sell_volume_usdt = sum(p * a for p, a in orderbook['asks'])
    
    # 十分な流動性がある場合のみ評価
    if total_buy_volume_usdt + total_sell_volume_usdt < 1_000_000:
         return 0.0, 0.0 

    imbalance_ratio = total_buy_volume_usdt / (total_buy_volume_usdt + total_sell_volume_usdt)

    score_impact = 0.0
    whale_imbalance_penalty_value = 0.0

    if side == 'ロング':
        if imbalance_ratio > WHALE_IMBALANCE_THRESHOLD:
            score_impact = WHALE_IMBALANCE_BONUS
        elif imbalance_ratio < 1.0 - WHALE_IMBALANCE_THRESHOLD:
            whale_imbalance_penalty_value = -WHALE_IMBALANCE_PENALTY
            score_impact = whale_imbalance_penalty_value

    return score_impact, whale_imbalance_penalty_value

def analyze_single_timeframe(df: pd.DataFrame, timeframe: str, symbol: str, macro_context: Dict) -> Optional[Dict]:
    """単一の時間足と銘柄に対して分析を実行する (v19.0.18)"""

    if df is None or len(df) < 50: 
        return None

    df_analyzed = calculate_indicators(df)
    last = df_analyzed.iloc[-1]
    prev = df_analyzed.iloc[-2]

    current_price = last['close']
    side = 'ロング'

    score = BASE_SCORE
    tech_data = {}

    # 1. 損切りの設定 (AvgRangeに基づくSL)
    avg_range = last['AvgRange']
    range_sl_amount = avg_range * RANGE_TRAIL_MULTIPLIER
    sl_price = current_price - range_sl_amount
    
    # 2. 構造的SLの検討 (Fibレベル)
    low_price = df_analyzed['low'].min()
    high_price = df_analyzed['high'].max()
    fib_levels = {
        '0.236': get_fibonacci_level(high_price, low_price, 0.236),
        '0.382': get_fibonacci_level(high_price, low_price, 0.382),
        '0.500': get_fibonacci_level(high_price, low_price, 0.500),
    }

    # 簡易的に、最も近いFibレベルを構造的SLとする
    closest_fib_level_price = min(fib_levels.values(), key=lambda x: abs(x - sl_price))
    
    structural_pivot_bonus = 0.0
    if abs(sl_price - closest_fib_level_price) / sl_price < 0.005: 
        structural_pivot_bonus = 0.05
    
    score += structural_pivot_bonus
    tech_data['structural_pivot_bonus'] = structural_pivot_bonus
    tech_data['fib_proximity_level'] = [k for k, v in fib_levels.items() if v == closest_fib_level_price][0] if structural_pivot_bonus else 'N/A'

    # 多重ピボットコンフルエンスボーナス
    multi_pivot_confluence_bonus_value = get_multi_pivot_confluence(sl_price, fib_levels, current_price, side)
    score += multi_pivot_confluence_bonus_value
    tech_data['multi_pivot_confluence_bonus_value'] = multi_pivot_confluence_bonus_value

    # 3. 利確の設定 (TP1 = SL幅のRRR倍)
    risk_amount_usd = current_price - sl_price
    tp1_price = current_price + (risk_amount_usd * DTS_RRR_DISPLAY)

    # 4. RRRによるボーナス
    rr_ratio = calculate_risk_reward_ratio(current_price, sl_price, tp1_price)
    rrr_bonus_value = 0.0
    if rr_ratio >= MIN_RRR_THRESHOLD:
        # RRRが3.0以上でボーナス
        rrr_bonus_value = RRR_BONUS_MULTIPLIER 
        score += rrr_bonus_value
        
    tech_data['rrr_bonus_value'] = rrr_bonus_value

    # --- スコアリング ---

    # 5. 長期トレンドフィルター (4h足の50SMA/EMA 200)
    long_term_reversal_penalty = False
    long_term_trend = 'N/A'
    if timeframe == '4h':
        sma_50 = last['SMA_50']
        ema_200 = last['EMA_200']
        is_uptrend = current_price > sma_50 and current_price > ema_200
        long_term_trend = 'Up' if is_uptrend else ('Down' if current_price < ema_200 else 'Sideways')
        tech_data['long_term_trend'] = long_term_trend

        if side == 'ロング' and long_term_trend == 'Down':
             score -= LONG_TERM_REVERSAL_PENALTY
             long_term_reversal_penalty = True
    elif timeframe == '1h':
        # 4hのトレンド情報を取得できない場合は、1hのEMA 200で簡易判断
        if current_price < last['EMA_200']:
            long_term_trend = 'Down (Proxy)'
            score -= LONG_TERM_REVERSAL_PENALTY * 0.5 
            long_term_reversal_penalty = True


    tech_data['long_term_reversal_penalty'] = long_term_reversal_penalty
    tech_data['long_term_trend'] = long_term_trend

    # 6. モメンタム (RSI, MACD, Stoch)

    # RSI (RSI 40以下からの反発でボーナス、60以上でペナルティ)
    rsi_overbought_penalty = 0
    rsi_support_touch_bonus = 0.0
    
    if last['RSI_14'] > RSI_MOMENTUM_LOW and prev['RSI_14'] <= RSI_MOMENTUM_LOW:
         rsi_support_touch_bonus = RSI_SUPPORT_TOUCH_BONUS
         score += rsi_support_touch_bonus
         
    # RSI過熱ペナルティ (v19.0.17から復元)
    rsi_overbought_penalty_value = 0.0
    if last['RSI_14'] > RSI_OVERBOUGHT_THRESHOLD: 
         score -= RSI_OVERBOUGHT_PENALTY
         rsi_overbought_penalty_value = -RSI_OVERBOUGHT_PENALTY
    
    tech_data['rsi_overbought_penalty'] = rsi_overbought_penalty_value
    tech_data['rsi_support_touch_bonus'] = rsi_support_touch_bonus
    
    # RSI弱気ダイバージェンスペナルティ
    rsi_divergence_penalty = check_rsi_divergence(df_analyzed, current_price, side)
    if rsi_divergence_penalty:
         score -= RSI_DIVERGENCE_PENALTY
         
    tech_data['rsi_divergence_penalty'] = rsi_divergence_penalty

    # MACDクロス確認 (ボーナス/ペナルティ)
    macd_cross_valid = last['MACDh_12_26_9'] > 0 and prev['MACDh_12_26_9'] < 0
    if macd_cross_valid:
         score += MACD_CROSS_BONUS
    else:
         score -= MACD_CROSS_PENALTY 

    tech_data['macd_cross_valid'] = macd_cross_valid

    # MACDダイバージェンスチェック (簡易版)
    macd_divergence_penalty_flag = last['MACDh_12_26_9'] < prev['MACDh_12_26_9'] and current_price > prev['close']
    if macd_divergence_penalty_flag:
         score -= MACD_DIVERGENCE_PENALTY
    tech_data['macd_divergence_penalty'] = macd_divergence_penalty_flag

    # Stochastics 
    stoch_k = last['STOCHk_14_3_3']
    stoch_d = last['STOCHd_14_3_3']
    stoch_filter_penalty = 0
    if stoch_k < 20 and stoch_d < 20:
         score += 0.05
    elif stoch_k > 80 or stoch_d > 80: 
         score -= 0.15
         stoch_filter_penalty = 1
    tech_data['stoch_filter_penalty'] = stoch_filter_penalty

    # 7. ADX (トレンドの強さ)
    adx = last['ADX']
    di_plus = last['DMP']
    di_minus = last['DMN']
    tech_data['adx'] = adx
    tech_data['di_plus'] = di_plus
    tech_data['di_minus'] = di_minus
    adx_strength_bonus_value = 0.0
    adx_absence_penalty = 0.0

    if adx > 25 and di_plus > di_minus and side == 'ロング':
        adx_strength_bonus_value = ADX_TREND_STRENGTH_BONUS
        score += adx_strength_bonus_value
    
    elif adx < ADX_ABSENCE_THRESHOLD:
        adx_absence_penalty = -ADX_TREND_ABSENCE_PENALTY
        score += adx_absence_penalty
        
    tech_data['adx_strength_bonus_value'] = adx_strength_bonus_value
    tech_data['adx_absence_penalty'] = adx_absence_penalty

    # 8. 出来高確認
    volume_confirmation_bonus = 0.0
    if last['volume'] > last['Volume_Avg_50'] * VOLUME_CONFIRMATION_MULTIPLIER:
        volume_confirmation_bonus = 0.05
        score += volume_confirmation_bonus
    tech_data['volume_confirmation_bonus'] = volume_confirmation_bonus

    # 9. 市場構造: VPVR出来高空隙ペナルティ
    vpvr_gap_penalty_flag = check_vpvr_gap_penalty(df_analyzed, sl_price, side)
    vpvr_gap_penalty_value = 0.0
    if vpvr_gap_penalty_flag:
        vpvr_gap_penalty_value = -VPVR_GAP_PENALTY
        score += vpvr_gap_penalty_value
        
    tech_data['vpvr_gap_penalty'] = vpvr_gap_penalty_value

    # 10. 流動性ボーナス & クジラバイアス
    liquidity_bonus_value = get_liquidity_bonus(symbol, current_price, side)
    score += liquidity_bonus_value
    tech_data['liquidity_bonus_value'] = liquidity_bonus_value
    
    whale_score_impact, whale_imbalance_penalty_value = get_whale_bias_score(symbol, current_price, side)
    score += whale_score_impact
    tech_data['whale_imbalance_bonus'] = whale_score_impact if whale_score_impact > 0 else 0.0
    tech_data['whale_imbalance_penalty'] = whale_imbalance_penalty_value if whale_imbalance_penalty_value < 0 else 0.0

    # 11. ボラティリティ（BBands）と価格構造 (VWAP)
    bb_width_percent = (last['BBU_5_2.0'] - last['BBL_5_2.0']) / last['close'] * 100
    bb_squeeze_ok = bb_width_percent < 5.0
    bb_wide_penalty_flag = bb_width_percent > 8.0
    
    if bb_squeeze_ok:
        score += BB_SQUEEZE_BONUS
    if bb_wide_penalty_flag:
        score -= BB_WIDE_PENALTY
        
    tech_data['bb_squeeze_ok'] = bb_squeeze_ok
    tech_data['bb_wide_penalty'] = bb_wide_penalty_flag

    vwap_confirm_ok = last['close'] > last['VWAP']
    if vwap_confirm_ok:
        score += VWAP_BONUS_POINT
    tech_data['vwap_confirm_ok'] = vwap_confirm_ok

    # 12. マクロセンチメント (FGI + FX要因の統合)
    
    # FGIプロキシ
    sentiment_fgi_proxy_value = macro_context.get('sentiment_fgi_proxy_value', 0.0)
    score += sentiment_fgi_proxy_value
    tech_data['sentiment_fgi_proxy_value'] = sentiment_fgi_proxy_value

    # FX要因
    dxy_surge_penalty = macro_context.get('dxy_surge_penalty', 0.0)
    usdjpy_risk_off_penalty = macro_context.get('usdjpy_risk_off_penalty', 0.0)
    fx_macro_stability_bonus = macro_context.get('usdjpy_stability_bonus', 0.0) 

    score += dxy_surge_penalty
    score += usdjpy_risk_off_penalty
    score += fx_macro_stability_bonus
    
    tech_data['dxy_surge_penalty'] = dxy_surge_penalty
    tech_data['usdjpy_risk_off_penalty'] = usdjpy_risk_off_penalty
    tech_data['fx_macro_stability_bonus'] = fx_macro_stability_bonus
    
    final_score = min(1.00, score)

    # リスク額の計算
    risk_amount_usdt = round(min(MAX_RISK_PER_TRADE_USDT, CURRENT_USDT_BALANCE * MAX_RISK_CAPITAL_PERCENT), 2)
    
    return {
        'symbol': symbol,
        'timeframe': timeframe,
        'side': side,
        'score': final_score,
        'score_raw': score,
        'entry_price': current_price,
        'rr_ratio': rr_ratio,
        'risk_amount_usdt': risk_amount_usdt,
        'trade_plan': {
            'sl_price': sl_price,
            'tp1_price': tp1_price,
            'risk_amount_usd_per_unit': risk_amount_usd
        },
        'tech_data': tech_data
    }

# ====================================================================================
# TRADING & POSITION MANAGEMENT
# ====================================================================================

async def execute_trade(signal: Dict):
    """シグナルに基づきポジションを取得する (シミュレーション)"""
    symbol = signal['symbol']
    
    ACTUAL_POSITIONS[symbol] = {
        'entry': signal['entry_price'],
        'sl': signal['trade_plan']['sl_price'],
        'tp': signal['trade_plan']['tp1_price'],
        'size': 1.0, 
        'status': 'open',
        'timestamp': time.time(),
        'side': signal['side']
    }
    
    notification_msg = (
        f"🤖 **自動取引実行通知**\n"
        f"銘柄: **{symbol}** | 方向: **{signal['side']}**\n"
        f"スコア: {signal['score'] * 100:.2f} | RRR: 1:{signal['rr_ratio']:.2f}\n"
        f"エントリー: ${format_price_utility(signal['entry_price'])}"
    )
    # 同期関数を非同期環境で呼び出す
    await asyncio.to_thread(send_position_status_notification, notification_msg)


async def check_and_execute_signals(signals: List[Dict]):
    """シグナルをチェックし、自動取引閾値を超えたものを実行する"""
    
    tradable_signals = [
        s for s in signals 
        if s['score'] >= TRADE_EXECUTION_THRESHOLD 
        and s['symbol'] not in ACTUAL_POSITIONS
    ]
    
    for signal in tradable_signals:
        symbol = signal['symbol']
        
        if time.time() - TRADE_COOLDOWN_TIMESTAMPS.get(symbol, 0) < TRADE_SIGNAL_COOLDOWN:
            continue
            
        if CURRENT_USDT_BALANCE < MIN_USDT_BALANCE_TO_TRADE:
            logging.warning(f"{symbol}: 残高不足のため取引をスキップしました。")
            continue
            
        await execute_trade(signal)
        TRADE_COOLDOWN_TIMESTAMPS[symbol] = time.time()

# ====================================================================================
# MAIN LOOP
# ====================================================================================

async def main_loop():
    """BOTのメイン処理ループ"""
    global LAST_ANALYSIS_SIGNALS, LAST_SUCCESS_TIME, CURRENT_USDT_BALANCE, FX_MACRO_CONTEXT
    
    while True:
        try:
            logging.info("--- メインループ開始 ---")
            
            # 1. 資産残高の更新
            CURRENT_USDT_BALANCE = await fetch_current_balance_usdt(EXCHANGE_CLIENT)
            logging.info(f"現在のUSDT残高: ${format_usdt(CURRENT_USDT_BALANCE)}")
            
            # 2. 監視銘柄の更新
            await update_symbols_by_volume(EXCHANGE_CLIENT)
            
            # 3. マクロコンテキストの取得 (FX/FGIを含む)
            FX_MACRO_CONTEXT = await get_fx_macro_context()
            logging.info(f"FX/マクロコンテキスト更新完了: {FX_MACRO_CONTEXT}")
            
            all_signals: List[Dict] = []
            
            # 4. 全銘柄の分析とシグナル生成
            for symbol in CURRENT_MONITOR_SYMBOLS:
                await fetch_order_book_depth(symbol) 
                
                for timeframe in ['1h', '4h']:
                    df_ohlcv, status, client = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, timeframe)
                    if status == "Success" and df_ohlcv is not None:
                        analysis_result = analyze_single_timeframe(df_ohlcv, timeframe, symbol, FX_MACRO_CONTEXT)
                        if analysis_result:
                            all_signals.append(analysis_result)

                await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL)
            
            LAST_ANALYSIS_SIGNALS = all_signals
            
            # 5. シグナルのソートとフィルタリング
            high_score_signals = sorted(
                [s for s in all_signals if s.get('score', 0.0) >= SIGNAL_THRESHOLD],
                key=lambda x: x.get('score', 0.0),
                reverse=True
            )
            
            # 6. 自動取引の実行
            if high_score_signals:
                await check_and_execute_signals(high_score_signals)

            # 7. 通知の送信 (スコア上位3つ)
            for i, signal in enumerate(high_score_signals[:TOP_SIGNAL_COUNT]):
                # クールダウンチェックは自動取引と共通
                if time.time() - TRADE_COOLDOWN_TIMESTAMPS.get(signal['symbol'], 0) < TRADE_SIGNAL_COOLDOWN:
                    continue
                
                message = format_integrated_analysis_message(signal['symbol'], high_score_signals, i + 1)
                if message and signal['score'] >= SIGNAL_THRESHOLD: 
                    # send_telegram_html は同期関数なので、非同期環境で呼び出す
                    await asyncio.to_thread(send_telegram_html, message)
                    TRADE_COOLDOWN_TIMESTAMPS[signal['symbol']] = time.time()
                    
            LAST_SUCCESS_TIME = time.time()
            logging.info(f"--- メインループ完了。次回の実行まで {LOOP_INTERVAL} 秒待機 ---")

        except Exception as e:
            logging.error(f"メインループで致命的なエラーが発生: {e}", exc_info=True)
            await asyncio.sleep(LOOP_INTERVAL * 2)

        await asyncio.sleep(LOOP_INTERVAL)

# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v19.0.18 - FX-Macro-Sensitivity Patch")

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v19.0.18 Startup initializing (FX-Macro-Sensitivity Patch)...")

    await initialize_ccxt_client()

    # ★ 修正箇所: send_position_status_notification は同期関数なのでawaitを削除
    send_position_status_notification("🤖 BOT v19.0.18 初回起動通知")

    global LAST_HOURLY_NOTIFICATION_TIME
    LAST_HOURLY_NOTIFICATION_TIME = time.time()

    # メインループを非同期タスクとして開始
    asyncio.create_task(main_loop())

@app.on_event("shutdown")
async def shutdown_event():
    global EXCHANGE_CLIENT
    if EXCHANGE_CLIENT:
        await EXCHANGE_CLIENT.close()
        logging.info("CCXTクライアントをシャットダウンしました。")

@app.get("/status")
def get_status():
    status_msg = {
        "status": "ok",
        "bot_version": "v19.0.18 - FX-Macro-Sensitivity Patch",
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "open_positions": len(ACTUAL_POSITIONS),
        "current_usdt_balance": CURRENT_USDT_BALANCE
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running.", "version": "v19.0.18 - FX-Macro-Sensitivity Patch"})

if __name__ == "__main__":
    # Renderのデプロイ環境に合わせてmain_render.pyというファイル名で実行されることを想定
    # (FastAPIの起動ファイル名に合わせる必要があるため、ここではuvicorn.runはコメントアウトまたは環境に合わせて調整してください)
    # uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
    pass
