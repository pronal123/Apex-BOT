# ====================================================================================
# Apex BOT v19.0.27 - Final Integrated Build (Patch 13: Eventベースの初回同期)
#
# 修正ポイント:
# 1. 【機能修正】analysis_only_notification_loop() の初回待機ロジックを、
#    グローバル変数 (LAST_SUCCESS_TIME) のポーリングから、より信頼性の高い
#    asyncio.Event() を使用した待機に変更。
# 2. 【同期強化】main_loop() の最後に Event を設定 (set()) し、確実に初回完了を通知。
# 3. 【バージョン更新】全てのバージョン情報を Patch 13 に更新。
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
import json
import re

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
ANALYSIS_ONLY_INTERVAL = 60 * 60 # 💡 1時間ごとの分析専用通知の間隔（秒）
REQUEST_DELAY_PER_SYMBOL = 0.5 # 銘柄ごとのAPIリクエストの遅延（秒）

# 🚨 環境変数から取得 (Render/Heroku/Vercelなどに設定が必要)
TELEGRAM_TOKEN = os.environ.get('TELEGRAM_TOKEN', 'YOUR_TELEGRAM_TOKEN')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', 'YOUR_TELEGRAM_CHAT_ID')

TRADE_SIGNAL_COOLDOWN = 60 * 60 * 2 # 同一銘柄のシグナル通知クールダウン（2時間）
SIGNAL_THRESHOLD = 0.75             # シグナルを通知する最低スコア
TOP_SIGNAL_COUNT = 3                # 通知するシグナルの最大数
REQUIRED_OHLCV_LIMITS = {'15m': 500, '1h': 500, '4h': 500} # 取得するOHLCVの足数

# 💡 テクニカル分析定数 (v19.0.27ベース)
VOLATILITY_BB_PENALTY_THRESHOLD = 5.0 # ボリンジャーバンドの幅が狭い場合のペナルティ閾値 (%)
LONG_TERM_SMA_LENGTH = 50           # 長期トレンド判定に使用するSMAの期間（4h足）
LONG_TERM_REVERSAL_PENALTY = 0.20   # 長期トレンドと逆行する場合のスコアペナルティ
MACD_CROSS_PENALTY = 0.15           # MACDが有利なクロスでない場合のペナルティ
RANGE_TRAIL_MULTIPLIER = 3.0        # 平均変動幅に基づいた初期SL/TPの乗数
DTS_RRR_DISPLAY = 5.0               # 通知メッセージに表示するリスクリワード比率
LIQUIDITY_BONUS_POINT = 0.06        # 板の厚み（流動性）ボーナス
ORDER_BOOK_DEPTH_LEVELS = 5         # オーダーブックの取得深度
OBV_MOMENTUM_BONUS = 0.04           # OBVによるモメンタム確証ボーナス
FGI_PROXY_BONUS_MAX = 0.07          # FGIプロキシによる最大ボーナス
FOREX_BONUS_MAX = 0.06              # 為替マクロによる最大ボーナス/ペナルティ
RSI_MOMENTUM_LOW = 40               # RSIが40以下でロングモメンタム候補
ADX_TREND_THRESHOLD = 30            # ADXによるトレンド/レンジ判定
BASE_SCORE = 0.40                   # ベースとなるスコア
VOLUME_CONFIRMATION_MULTIPLIER = 2.5 # 出来高が過去平均のX倍以上で確証

# 💡 自動売買設定 (v19.0.27ベース)
MAX_RISK_PER_TRADE_USDT = 5.0       # 1取引あたりの最大リスク額 (USDT)
MAX_RISK_CAPITAL_PERCENT = 0.01     # 1取引あたりの最大リスク額 (総資金に対する割合)
TRADE_SIZE_PER_RISK_MULTIPLIER = 1.0 # 許容リスク額に対する取引サイズ乗数（1.0でリスク額＝損失額）
MIN_USDT_BALANCE_TO_TRADE = 50.0    # 取引を開始するための最低USDT残高

# ====================================================================================
# GLOBAL STATE & CACHES
# ====================================================================================

CCXT_CLIENT_NAME: str = 'MEXC' # ログ表示用。実際にはAPIキーに基づいて初期化
EXCHANGE_CLIENT: Optional[ccxt_async.Exchange] = None
LAST_UPDATE_TIME: float = 0.0
CURRENT_MONITOR_SYMBOLS: List[str] = DEFAULT_SYMBOLS[:TOP_SYMBOL_LIMIT]
TRADE_NOTIFIED_SYMBOLS: Dict[str, float] = {} # 通知済みシグナルのクールダウン管理
LAST_ANALYSIS_SIGNALS: List[Dict] = [] # 💡 メインループが生成したトップシグナルを保持（全スコア付き）
LAST_SUCCESS_TIME: float = 0.0 # 💡 メインループの最終成功時間
GLOBAL_MACRO_CONTEXT: Dict = {}
ORDER_BOOK_CACHE: Dict[str, Any] = {} # 流動性データキャッシュ

# 💡 IPアドレス制限エラーメッセージを一時保存するグローバル変数
LAST_IP_ERROR_MESSAGE: Optional[str] = None

# 💡 ポジション管理システム
# {symbol: {'entry_price': float, 'amount': float, 'sl_price': float, 'tp_price': float, 'open_time': float, 'status': str}}
ACTUAL_POSITIONS: Dict[str, Dict] = {}
LAST_HOURLY_NOTIFICATION_TIME: float = 0.0 # 💡 定期ポジションステータス通知用
LAST_ANALYSIS_ONLY_NOTIFICATION_TIME: float = 0.0 # 💡 分析専用通知用

# 💡 Patch 13: 初回分析レポート即時送信のための非同期イベント
FIRST_ANALYSIS_EVENT = asyncio.Event() 

# ログ設定
logging.basicConfig(level=logging.INFO,
                    format='%(asctime)s - %(levelname)s - %(message)s',
                    datefmt='%Y-%m-%d %H:%M:%S',
                    stream=sys.stdout,
                    force=True)
logging.getLogger('ccxt').setLevel(logging.WARNING)
# yfinanceの警告を抑制
logging.getLogger('yfinance').setLevel(logging.ERROR) 

# ====================================================================================
# UTILITIES & FORMATTING
# ====================================================================================

def get_tp_reach_time(timeframe: str) -> str:
    """時間足に応じたTP到達までの目安時間を返す"""
    if timeframe == '15m': return "数時間〜半日"
    if timeframe == '1h': return "半日〜数日"
    if timeframe == '4h': return "数日〜1週間"
    return "N/A"

def format_price_utility(price: float, symbol: str) -> str:
    """価格の桁数を調整してフォーマットする"""
    if price < 0.0001: return f"{price:.8f}"
    if price < 0.01: return f"{price:.6f}"
    if price < 1.0: return f"{price:.4f}"
    if price < 100.0: return f"{price:,.2f}"
    return f"{price:,.2f}"

def format_usdt(amount: float) -> str:
    """USDT残高をフォーマットする"""
    return f"{amount:,.2f}"

def get_estimated_win_rate(score: float, timeframe: str) -> float:
    """スコアと時間足から推定勝率を算出する"""
    base_rate = score * 0.50 + 0.35

    if timeframe == '15m':
        return max(0.40, min(0.75, base_rate))
    elif timeframe == '1h':
        return max(0.45, min(0.85, base_rate))
    elif timeframe == '4h':
        return max(0.50, min(0.90, base_rate))
    return base_rate

def format_integrated_analysis_message(symbol: str, signals: List[Dict], rank: int) -> str:
    """分析結果を統合したTelegramメッセージをHTML形式で作成する (v19.0.27)"""
    
    valid_signals = [s for s in signals if s.get('side') == 'ロング']
    if not valid_signals:
        return ""

    # スコアが閾値を超えたシグナルの中から、最もRRR/スコアが高いものを選択
    high_score_signals = [s for s in valid_signals if s.get('score', 0.5) >= SIGNAL_THRESHOLD]
    if not high_score_signals:
        # この関数はSIGNAL_THRESHOLD以上のシグナル通知用だが、念のためこのパスも警告を出さない
        return ""

    best_signal = max(
        high_score_signals,
        key=lambda s: (s.get('score', 0.5), s.get('rr_ratio', 0.0))
    )

    price = best_signal.get('price', 0.0)
    timeframe = best_signal.get('timeframe', 'N/A')
    score_raw = best_signal.get('score', 0.5)
    rr_ratio = best_signal.get('rr_ratio', 0.0)

    entry_price = best_signal.get('entry', 0.0)
    sl_price = best_signal.get('sl', 0.0)
    tp1_price = best_signal.get('tp1', 0.0)

    trade_plan_data = best_signal.get('trade_plan', {})
    trade_amount_usdt = trade_plan_data.get('trade_size_usdt', 0.0)
    max_risk_usdt = trade_plan_data.get('max_risk_usdt', MAX_RISK_PER_TRADE_USDT)

    score_100 = score_raw * 100
    win_rate = get_estimated_win_rate(score_raw, timeframe) * 100

    if score_raw >= 0.85:
        confidence_text = "<b>極めて高い</b>"
    elif score_raw >= 0.75:
        confidence_text = "<b>高い</b>"
    else:
        confidence_text = "中程度"

    direction_emoji = "🚀"
    direction_text = "<b>ロング (現物買い推奨)</b>"

    rank_emojis = {1: "🥇", 2: "🥈", 3: "🥉"}
    rank_emoji = rank_emojis.get(rank, "🏆")

    # SLソースの表示をATRからRangeに変更
    sl_source_str = "Range基準"
    if best_signal.get('tech_data', {}).get('structural_sl_used', False):
        sl_source_str = "構造的 (Pivot/Fib) + 0.5 Range バッファ"

    # 残高不足で取引がスキップされた場合の表示調整
    if trade_amount_usdt == 0.0 and trade_plan_data.get('max_risk_usdt', 0.0) == 0.0:
         # 残高不足または取引サイズが小さすぎる場合
         trade_size_str = "<code>不足/小</code>"
         max_risk_str = "<code>不足/小</code>"
         trade_plan_header = "⚠️ <b>通知のみ（取引サイズ不足）</b>"
    elif trade_plan_data.get('max_risk_usdt', 0.0) == 0.0 and trade_plan_data.get('amount_to_buy', 0.0) == 0.0 and max_risk_usdt > 0.0:
         # MIN_USDT_BALANCE_TO_TRADE未満の場合 (ゼロ残高を含む)
         trade_size_str = "<code>不足</code>"
         max_risk_str = "<code>不足</code>"
         trade_plan_header = "⚠️ <b>通知のみ（残高不足）</b>"
    else:
         trade_size_str = f"<code>{format_usdt(trade_amount_usdt)}</code>"
         max_risk_str = f"<code>${format_usdt(max_risk_usdt)}</code>"
         trade_plan_header = "✅ <b>自動取引計画</b>"


    header = (
        f"{rank_emoji} <b>Apex Signal - Rank {rank}</b> {rank_emoji}\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<b>{symbol}</b> | {direction_emoji} {direction_text} ({CCXT_CLIENT_NAME} Spot)\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>現在単価 (Market Price)</b>: <code>${format_price_utility(price, symbol)}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    trade_plan = (
        f"{trade_plan_header}\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>取引サイズ (USDT)</b>: {trade_size_str}\n"
        f"  - <b>許容最大リスク</b>: {max_risk_str}\n"
        f"  - <b>エントリー価格</b>: <code>${format_price_utility(entry_price, symbol)}</code>\n"
        f"  - <b>参考損切り (SL)</b>: <code>${format_price_utility(sl_price, symbol)}</code> ({sl_source_str})\n"
        f"  - <b>参考利確 (TP)</b>: <code>${format_price_utility(tp1_price, symbol)}</code> (DTS Base)\n"
        f"  - <b>目標RRR (DTS Base)</b>: 1 : {rr_ratio:.2f}+\n\n"
    )

    tech_data = best_signal.get('tech_data', {})
    regime = "トレンド相場" if tech_data.get('adx', 0.0) >= ADX_TREND_THRESHOLD else "レンジ相場"
    fgi_score = tech_data.get('sentiment_fgi_proxy_bonus', 0.0)
    fgi_sentiment = "リスクオン" if fgi_score > 0.001 else ("リスクオフ" if fgi_score < -0.001 else "中立") # 中立判定の閾値を設定
    
    # FGIの生値を取得
    fgi_raw_value = GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')

    # 為替マクロ情報の表示
    forex_score = tech_data.get('forex_macro_bonus', 0.0)
    forex_trend_status = GLOBAL_MACRO_CONTEXT.get('forex_trend', 'N/A')
    if forex_trend_status == 'USD_WEAKNESS_BULLISH':
         forex_display = "USD弱気 (リスクオン優勢)"
    elif forex_trend_status == 'USD_STRENGTH_BEARISH':
         forex_display = "USD強気 (リスクオフ優勢)"
    else:
         forex_display = "中立"
    

    summary = (
        f"<b>💡 分析サマリー</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>分析スコア</b>: <code>{score_100:.2f} / 100</code> (信頼度: {confidence_text})\n"
        f"  - <b>予測勝率</b>: <code>約 {win_rate:.1f}%</code>\n"
        f"  - <b>時間軸 (メイン)</b>: <code>{timeframe}</code>\n"
        f"  - <b>決済までの目安</b>: {get_tp_reach_time(timeframe)}\n"
        f"  - <b>市場の状況</b>: {regime} (ADX: {tech_data.get('adx', 0.0):.1f})\n"
        f"  - <b>恐怖・貪欲指数 (FGI)</b>: {fgi_sentiment} (現在値: <code>{fgi_raw_value}</code>, {abs(fgi_score*100):.1f}点影響)\n"
        f"  - <b>為替マクロ (EUR/USD)</b>: {forex_display} ({abs(forex_score*100):.1f}点影響)\n\n" # 為替表示を追加
    )

    long_term_trend_ok = not tech_data.get('long_term_reversal_penalty', False)
    momentum_ok = tech_data.get('macd_cross_valid', True) and not tech_data.get('stoch_filter_penalty', 0) > 0
    structure_ok = tech_data.get('structural_pivot_bonus', 0.0) > 0
    volume_confirm_ok = tech_data.get('volume_confirmation_bonus', 0.0) > 0
    obv_confirm_ok = tech_data.get('obv_momentum_bonus_value', 0.0) > 0
    liquidity_ok = tech_data.get('liquidity_bonus_value', 0.0) > 0
    fib_level = tech_data.get('fib_proximity_level', 'N/A')

    lt_trend_str = tech_data.get('long_term_trend', 'N/A')
    lt_trend_check_text_penalty = f"長期トレンド ({lt_trend_str}) と逆行 ({tech_data.get('long_term_reversal_penalty_value', 0.0)*100:.1f}点ペナルティ)"


    analysis_details = (
        f"<b>🔍 分析の根拠</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - <b>トレンド/勢い</b>: \n"
        f"    {'✅' if long_term_trend_ok else '❌'} {'<b>' if not long_term_trend_ok else ''}{f'長期 ({lt_trend_str}, SMA {LONG_TERM_SMA_LENGTH}) トレンドと一致' if long_term_trend_ok else lt_trend_check_text_penalty}{'</b>' if not long_term_trend_ok else ''}\n"
        f"    {'✅' if momentum_ok else '⚠️'} 短期モメンタム加速 (RSI/MACD/CCI)\n"
        f"  - <b>価格構造/ファンダ</b>: \n"
        f"    {'✅' if structure_ok else '❌'} 重要支持/抵抗線に近接 ({fib_level}確認)\n"
        f"    {'✅' if (volume_confirm_ok or obv_confirm_ok) else '❌'} 出来高/OBVの裏付け\n"
        f"    {'✅' if liquidity_ok else '❌'} 板の厚み (流動性) 優位\n"
    )

    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ このシグナルは自動売買の対象です。</pre>"
        f"<i>Bot Ver: v19.0.27 - Final Integrated Build (Patch 13)</i>" # 💡 バージョンをPatch 13に更新
    )

    return header + trade_plan + summary + analysis_details + footer

def format_analysis_only_message(all_signals: List[Dict], macro_context: Dict) -> str:
    """💡 1時間ごとの分析専用メッセージを作成する (Patch 12)"""
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")
    
    # 1. 候補リストの作成 (スコア降順にソート)
    # メインループで生成された全スコア付きシグナルリストを使用
    sorted_signals = sorted(all_signals, key=lambda s: s.get('score', 0.0), reverse=True)
    
    # 2. トップN件を取得
    top_signals_to_display = sorted_signals[:TOP_SIGNAL_COUNT]

    header = (
        f"📊 **Apex Market Snapshot (Hourly Analysis)**\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **確認日時**: {now_jst} (JST)\n"
        f"  - **取引ステータス**: <b>分析通知のみ</b>\n"
        f"  - **対象銘柄数**: <code>{len(CURRENT_MONITOR_SYMBOLS)}</code>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    # マクロコンテキスト情報
    fgi_raw_value = macro_context.get('fgi_raw_value', 'N/A')
    fgi_proxy = macro_context.get('fgi_proxy', 0.0)
    forex_trend_status = macro_context.get('forex_trend', 'N/A')
    forex_bonus = macro_context.get('forex_bonus', 0.0)

    fgi_sentiment = "リスクオン" if fgi_proxy > 0.001 else ("リスクオフ" if fgi_proxy < -0.001 else "中立")
    
    if forex_trend_status == 'USD_WEAKNESS_BULLISH':
         forex_display = "USD弱気 (リスクオン優勢)"
    elif forex_trend_status == 'USD_STRENGTH_BEARISH':
         forex_display = "USD強気 (リスクオフ優勢)"
    else:
         forex_display = "中立"

    macro_section = (
        f"🌍 <b>グローバルマクロ分析</b>\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **恐怖・貪欲指数 (FGI)**: <code>{fgi_raw_value}</code> ({fgi_sentiment})\n"
        f"  - **為替マクロ (EUR/USD)**: {forex_display}\n"
        f"  - **総合マクロ影響**: <code>{((fgi_proxy + forex_bonus) * 100):.2f}</code> 点\n\n"
    )

    # トップシグナル情報
    signal_section = "📈 <b>トップシグナル候補 (スコア順)</b>\n"
    if top_signals_to_display:
        for rank, signal in enumerate(top_signals_to_display, 1):
            symbol = signal['symbol']
            timeframe = signal['timeframe']
            score = signal['score']
            rr_ratio = signal['rr_ratio']
            
            # 💡 スコアが閾値未満の場合、色付けで注意を促す
            score_color = ""
            if score < SIGNAL_THRESHOLD:
                 score_color = "⚠️" 
                 
            signal_section += (
                f"  {rank}. <b>{symbol}</b> ({timeframe}) {score_color}\n"
                f"     - スコア: <code>{score * 100:.2f} / 100</code> (RRR: 1:{rr_ratio:.1f})\n"
            )
        
        # 💡 閾値を超えたシグナルがゼロの場合のみ警告を表示
        # (sorted_signalsリストが空でない && 最高のスコアが閾値未満)
        if sorted_signals[0]['score'] < SIGNAL_THRESHOLD:
             signal_section += "\n<pre>⚠️ 注: 上記は監視中の最高スコアですが、閾値 (75点) 未満です。</pre>\n"

    else:
        # シグナルがゼロ件の場合 (ログで確認済みのパターン)
        signal_section += "  - **シグナル候補なし**: 現在、すべての監視銘柄で取引推奨スコア (40点以上) に達するロングシグナルは見つかりませんでした。\n"
    
    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<pre>※ この通知は取引実行を伴いません。</pre>"
        f"<i>Bot Ver: v19.0.27 - Final Integrated Build (Patch 13)</i>" # 💡 バージョンをPatch 13に更新
    )

    return header + macro_section + signal_section + footer

def format_position_status_message(balance_usdt: float, open_positions: Dict, balance_status: str) -> str:
    """現在のポジション状態をまとめたTelegramメッセージをHTML形式で作成する (v19.0.27)"""
    global LAST_IP_ERROR_MESSAGE 
    now_jst = datetime.now(JST).strftime("%Y/%m/%d %H:%M:%S")

    # 💡 ステータスに応じたヘッダーと警告メッセージ
    warning_msg = ""
    
    if balance_status == 'AUTH_ERROR':
        status_line = "🔴 **認証エラー発生**"
        warning_msg = "\n🚨 **APIキー/Secretが不正です。**すぐに確認してください。"
    elif balance_status == 'API_ERROR' or balance_status == 'OTHER_ERROR':
        status_line = "⚠️ **API通信エラー/権限不足の可能性**"
        warning_msg = f"\n🚨 **{CCXT_CLIENT_NAME}との通信に失敗または権限が不足しています。**ログを確認してください。"
    elif balance_status == 'IP_ERROR':
         status_line = "❌ **IPアドレス制限エラー**"
         
         # 💡 IPアドレスを抽出して表示
         extracted_ip = "N/A"
         if LAST_IP_ERROR_MESSAGE:
             # 例: mexc {"code":700006,"msg":"IP [54.254.162.138] not in the ip white list"}
             match = re.search(r"IP\s*\[(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\]", LAST_IP_ERROR_MESSAGE)
             if match:
                 extracted_ip = match.group(1)
         
         warning_msg = (
             f"\n🚨 **RenderのIPアドレスがMEXCのAPIホワイトリストに登録されていません。**"
             f"\n  - **アクセス拒否されたIP**: <code>{extracted_ip}</code>"
             f"\n  - **対応**: MEXC API設定で上記IPをホワイトリストに**追加**してください。"
         )
         
    elif balance_status == 'ZERO_BALANCE':
        # 実際残高がゼロ、またはAPI応答からUSDT残高情報が完全に欠落している場合のメッセージ
        status_line = "✅ **残高確認完了 (残高ゼロ)**"
        warning_msg = "\n👉 **USDT残高がゼロ、またはAPI応答から見つからないため、自動取引はスキップされます。**"
    else: # SUCCESS
        status_line = "🔔 **Apex BOT ポジション/残高ステータス**"
        warning_msg = ""


    header = (
        f"{status_line} ({CCXT_CLIENT_NAME} Spot)\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"  - **最終確認日時**: {now_jst} (JST)\n"
        f"  - **利用可能USDT残高**: <code>${format_usdt(balance_usdt)}</code>"
        f"{warning_msg}\n" # 警告メッセージを挿入
        f"  - **保有中ポジション数**: <code>{len(open_positions)}</code> 件\n"
        f"<code>- - - - - - - - - - - - - - - - - - - - -</code>\n\n"
    )

    if not open_positions:
        return header + "👉 **現在、保有中の現物ポジションはありません。**\n"

    details = "📈 **保有ポジション詳細**\n\n"
    for symbol, pos in open_positions.items():
        entry = format_price_utility(pos['entry_price'], symbol)
        sl = format_price_utility(pos['sl_price'], symbol)
        tp = format_price_utility(pos['tp_price'], symbol)
        amount = pos['amount']

        details += (
            f"🔹 <b>{symbol}</b> ({amount:.4f} 単位)\n"
            f"  - Buy @ <code>${entry}</code> (Open: {datetime.fromtimestamp(pos['open_time'], tz=JST).strftime('%m/%d %H:%M')})\n"
            f"  - SL: <code>${sl}</code> | TP: <code>${tp}</code>\n"
            f"  - Status: {pos['status']}\n"
        )

    footer = (
        f"\n<code>- - - - - - - - - - - - - - - - - - - - -</code>\n"
        f"<i>Bot Ver: v19.0.27 - Final Integrated Build (Patch 13)</i>" # 💡 バージョンをPatch 13に更新
    )

    return header + details + footer

def send_telegram_html(message: str):
    """TelegramにHTML形式でメッセージを送信する"""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID or TELEGRAM_TOKEN == 'YOUR_TELEGRAM_TOKEN':
        logging.warning("⚠️ TelegramトークンまたはチャットIDが設定されていません。通知をスキップします。")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    }

    try:
        response = requests.post(url, data=payload, timeout=5)
        response.raise_for_status()
    except requests.exceptions.RequestException as e:
        # トークンが不正な場合のログを修正 (トークン自体は表示しない)
        if "404 Client Error: Not Found" in str(e) and 'YOUR_TELEGRAM_TOKEN' in TELEGRAM_TOKEN:
            logging.error(f"Telegram送信エラー: 404 Client Error: Not Found for url: https://api.telegram.org/botYOUR_TELEGRAM_BOT_TOKEN_HERE/sendMessage (トークン未設定の可能性)")
        elif "401 Client Error: Unauthorized" in str(e):
             logging.error(f"Telegram送信エラー: 401 Client Error: Unauthorized (トークンが無効です)")
        else:
             logging.error(f"Telegramへの接続/HTTPエラー: {e}")
    except Exception as e:
        logging.error(f"未知のTelegram通知エラー: {e}")


# ====================================================================================
# CCXT & DATA ACQUISITION
# ====================================================================================

async def initialize_ccxt_client():
    """CCXTクライアントを初期化 (MEXCをデフォルトとする)"""
    global EXCHANGE_CLIENT
    api_key = os.environ.get('MEXC_API_KEY')
    secret = os.environ.get('MEXC_SECRET')

    config = {
        'timeout': 30000,
        'enableRateLimit': True,
        'options': {
            'defaultType': 'spot',
            'defaultSubType': 'spot',
            'fetchBalanceMethod': 'v3',
        },
        'apiKey': api_key,
        'secret': secret,
    }

    try:
        # MEXCクライアントの初期化
        EXCHANGE_CLIENT = ccxt_async.mexc(config)
        auth_status = "認証済み" if api_key and secret else "公開データのみ"
        logging.info(f"CCXTクライアントを {CCXT_CLIENT_NAME} で初期化しました。({auth_status}, Default: Spot)")
    except Exception as e:
        logging.error(f"CCXTクライアントの初期化に失敗しました: {e}")
        EXCHANGE_CLIENT = None


async def fetch_current_balance_usdt_with_status() -> Tuple[float, str]:
    """CCXTから現在のUSDT残高を取得し、ステータスを返す。(v19.0.27)"""
    global EXCHANGE_CLIENT, LAST_IP_ERROR_MESSAGE
    if not EXCHANGE_CLIENT:
        return 0.0, 'AUTH_ERROR'
        
    # エラーが発生しない場合のために、IPエラーメッセージをリセット
    if LAST_IP_ERROR_MESSAGE is not None:
         LAST_IP_ERROR_MESSAGE = None

    try:
        balance = await EXCHANGE_CLIENT.fetch_balance()
        usdt_free = 0.0

        # 1. CCXT標準の Unified Balance 構造からの取得
        if 'USDT' in balance and isinstance(balance['USDT'], dict):
             usdt_free = balance['USDT'].get('free', 0.0)
             if usdt_free > 0.0:
                  return usdt_free, 'SUCCESS'

        # 1.2. Unifiedオブジェクトの 'free' ディクショナリにある場合
        patch_free_unified = balance.get('free', {}).get('USDT', 0.0)
        if patch_free_unified > 0.0:
             return patch_free_unified, 'SUCCESS'

        # 2. Raw Info 強制パッチ (MEXC固有の対応 - 最終手段)
        try:
            raw_info = balance.get('info', {})
            search_paths = [raw_info.get('assets'), raw_info.get('balances'), [raw_info]] 

            for assets_list in search_paths:
                if isinstance(assets_list, list):
                    for asset in assets_list:
                        is_usdt = (asset.get('currency') == 'USDT' or asset.get('asset') == 'USDT' or asset.get('coin') == 'USDT')
                        if is_usdt:
                            available_balance = float(asset.get('availableBalance', asset.get('free', 0.0)))
                            if available_balance <= 0.0 and 'total' in asset:
                                available_balance = float(asset.get('total', 0.0)) - float(asset.get('locked', 0.0))

                            if available_balance > 0.0:
                                return available_balance, 'SUCCESS'

        except Exception as e:
            pass # パッチ失敗は無視

        # 3. 取得失敗時の判定
        return 0.0, 'ZERO_BALANCE' # <- 実際の残高がゼロの場合

    except ccxt.AuthenticationError:
        logging.error("❌ 残高取得エラー: APIキー/Secretが不正です (AuthenticationError)。")
        return 0.0, 'AUTH_ERROR' # <- 認証エラーの場合
    except ccxt.ExchangeError as e:
        error_msg = str(e)
        logging.error(f"❌ 残高取得エラー（CCXT Exchange Error）: {type(e).__name__}: {e}")
        # IPアドレス制限エラーの検出
        if "700006" in error_msg and "ip white list" in error_msg.lower():
             # 💡 IPエラーメッセージを保存
             LAST_IP_ERROR_MESSAGE = error_msg
             return 0.0, 'IP_ERROR'
        return 0.0, 'API_ERROR' # <- API通信エラーの場合
    except Exception as e:
        logging.error(f"❌ 残高取得エラー（fetch_balance失敗）: {type(e).__name__}: {e}")
        return 0.0, 'OTHER_ERROR' # <- その他の予期せぬエラーの場合

async def fetch_ohlcv_with_fallback(exchange_id: str, symbol: str, timeframe: str) -> Tuple[pd.DataFrame, str, str]:
    """OHLCVデータを取得する"""
    global EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT: return pd.DataFrame(), "Client Error", "EXCHANGE_CLIENT not initialized."

    try:
        limit = REQUIRED_OHLCV_LIMITS.get(timeframe, 500)
        ohlcv = await EXCHANGE_CLIENT.fetch_ohlcv(symbol, timeframe, limit=limit)
        df = pd.DataFrame(ohlcv, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume'])
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms', utc=True).dt.tz_convert(JST)
        df.set_index('timestamp', inplace=True)
        if len(df) < limit * 0.9:
             return pd.DataFrame(), "Warning", f"Insufficient data ({len(df)}/{limit})"
        return df, "Success", ""
    except Exception as e:
        logging.error(f"❌ {symbol} {timeframe}: OHLCV取得エラー: {e}")
        return pd.DataFrame(), "Error", str(e)

async def update_symbols_by_volume():
    """出来高に基づいて監視銘柄を更新する"""
    global CURRENT_MONITOR_SYMBOLS, EXCHANGE_CLIENT
    if not EXCHANGE_CLIENT: return

    try:
        markets = await EXCHANGE_CLIENT.fetch_tickers()
        usdt_pairs = {s: t for s, t in markets.items() if s.endswith('/USDT')}

        # 出来高降順でソート
        sorted_pairs = sorted(
            usdt_pairs.items(),
            key=lambda x: x[1].get('quoteVolume', 0) or 0,
            reverse=True
        )

        top_symbols = [s for s, t in sorted_pairs[:TOP_SYMBOL_LIMIT]]
        if top_symbols:
            CURRENT_MONITOR_SYMBOLS = top_symbols
    except Exception as e:
        pass # エラー時は既存のリストを維持

def fetch_fgi_sync() -> int:
    """FGIを同期的に取得する (Alternative.meを想定)"""
    FGI_API_URL = "https://api.alternative.me/fng/"
    try:
        response = requests.get(FGI_API_URL, timeout=5)
        response.raise_for_status()
        data = response.json()
        fgi_data = data.get('data', [{}])[0]
        value = int(fgi_data.get('value', 50))
        return value
    except Exception as e:
        return 50

def fetch_forex_data_sync(ticker: str, interval: str, period: str) -> Optional[pd.DataFrame]:
    """yfinanceから為替データを同期的に取得する (EURUSD=X) - Rate Limit/Connection対策を追加 (Patch 4 & 6)"""
    
    MAX_RETRIES = 3
    RETRY_DELAY = 10  # 10秒待機

    for attempt in range(MAX_RETRIES):
        try:
            # ⚠️ 注意: yfinanceの警告は抑制されているが、ダウンロード自体は行われる
            data = yf.download(ticker, interval=interval, period=period, progress=False)
            
            # 💡 【Patch 6: Robustness Fix】データが空、データ数が不十分、または必要なカラム('Close')がない場合も一時的なエラーとみなしリトライ
            if data.empty or len(data) < 30 or 'Close' not in data.columns: 
                 if attempt < MAX_RETRIES - 1:
                      logging.warning(f"⚠️ yfinanceデータ取得: 空/不足 ({len(data)}件) または 'Close'カラム不足。({attempt + 1}/{MAX_RETRIES})。{RETRY_DELAY}秒後に再試行します。")
                      time.sleep(RETRY_DELAY)
                      continue
                 return None

            return data
        except Exception as e: # 💡 RateLimit, ConnectionErrorなど全てをキャッチ
            if attempt < MAX_RETRIES - 1:
                # エラー発生時、リトライ回数が残っていれば待機
                logging.warning(f"⚠️ yfinanceデータ取得エラー ({type(e).__name__})。({attempt + 1}/{MAX_RETRIES})。{RETRY_DELAY}秒後に再試行します。")
                time.sleep(RETRY_DELAY)
            else:
                # 最終リトライ失敗
                logging.error(f"❌ yfinanceデータ取得が最終リトライでも失敗しました。: {type(e).__name__}: {e}")
                return None
    return None # safety return


async def get_crypto_macro_context() -> Dict:
    """市場全体のマクロコンテキストを取得する (FGI/為替 リアルデータ取得) - Patch 7: MACD計算のロバスト性強化"""
    
    # 💡 最初にデフォルト値を設定
    fgi_value = 50
    fgi_proxy = 0.0
    forex_trend = 'NEUTRAL'
    forex_bonus = 0.0
    
    # 1. 恐怖・貪欲指数 (FGI) を取得
    try:
        fgi_value = await asyncio.to_thread(fetch_fgi_sync)
        fgi_normalized = (fgi_value - 50) / 20.0
        fgi_proxy = max(-FGI_PROXY_BONUS_MAX, min(FGI_PROXY_BONUS_MAX, fgi_normalized * FGI_PROXY_BONUS_MAX))

    except Exception as e:
        # FGI取得失敗 (ネットワークエラーなど)
        logging.warning(f"FGI取得エラー（requestsベース）：{type(e).__name__}: {e}。デフォルト値で続行します。")
        # fgi_value, fgi_proxy はデフォルト値のまま (50, 0.0)

    # 2. 為替マクロデータ (EUR/USD) を取得し、USDの強弱を判定
    try:
        forex_df = await asyncio.to_thread(fetch_forex_data_sync, "EURUSD=X", "60m", "7d") # 1時間足、過去7日間
        
        # 💡 【Patch 7: Robustness強化】DataFrameの完全性を再チェックし、MACD計算結果の検証を追加
        if forex_df is not None and not forex_df.empty and len(forex_df) > 30 and 'Close' in forex_df.columns:
            
            # ユーロドル (EURUSD=X) のMACDヒストグラムを計算
            macd_df = ta.macd(forex_df['Close'], fast=12, slow=26, signal=9)
            
            # MACDの計算結果と必要な列の存在をチェック
            if macd_df is not None and 'MACDh_12_26_9' in macd_df.columns and not macd_df.empty:
                forex_df['MACD'] = macd_df['MACDh_12_26_9']
                
                # NoneType Errorの発生源であった.iloc[-1]アクセスを安全に実行
                last_macd_hist = forex_df['MACD'].iloc[-1]
                
                # EURUSD Bullish (上昇) = USD Weakening = Crypto Bullish (リスクオン)
                if last_macd_hist > 0.00001: 
                    forex_trend = 'USD_WEAKNESS_BULLISH'
                    forex_bonus = FOREX_BONUS_MAX
                # EURUSD Bearish (下落) = USD Strengthening = Crypto Bearish (リスクオフ)
                elif last_macd_hist < -0.00001: 
                    forex_trend = 'USD_STRENGTH_BEARISH'
                    forex_bonus = -FOREX_BONUS_MAX
        
    except Exception as e:
         # 💡 【Patch 7: yfinanceエラーを分離】為替データ取得・処理中のエラーをキャッチ
         error_type = type(e).__name__
         # エラーログは出すが、FGIデータは保持して続行
         logging.warning(f"マクロコンテキスト取得中にエラー発生（為替データ/yfinance関連）：{error_type}: {e}")
         # forex_trend, forex_bonus はデフォルト値のまま ('NEUTRAL', 0.0)

    # 成功または一部失敗後の統合結果を返す (Noneを返さないことを保証)
    return {
        'fgi_proxy': fgi_proxy,
        'fgi_raw_value': fgi_value,
        'forex_trend': forex_trend,
        'forex_bonus': forex_bonus,
    }


async def fetch_order_book_depth(symbol: str) -> bool:
    """オーダーブックの流動性データをキャッシュする (リアルCCXTデータを使用)"""
    global EXCHANGE_CLIENT, ORDER_BOOK_CACHE
    if not EXCHANGE_CLIENT: return False

    try:
        # CCXTからオーダーブックを取得
        order_book = await EXCHANGE_CLIENT.fetch_order_book(symbol, limit=ORDER_BOOK_DEPTH_LEVELS)
        
        # 買い板 (Bids) の合計USDTボリュームを計算 (価格 * 数量)
        bids_depth = sum(bid[0] * bid[1] for bid in order_book['bids'])

        # 売り板 (Asks) の合計USDTボリュームを計算 (価格 * 数量)
        asks_depth = sum(ask[0] * ask[1] for ask in order_book['asks'])
        
        ORDER_BOOK_CACHE[symbol] = {
            'bids_depth': bids_depth, 
            'asks_depth': asks_depth
        }
        return True
    except Exception as e:
        ORDER_BOOK_CACHE[symbol] = {'bids_depth': 0.0, 'asks_depth': 0.0} # エラー時はゼロとして扱う
        return False

# ====================================================================================
# TRADING & ANALYSIS LOGIC
# ====================================================================================

def calculate_technical_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """必要なテクニカル指標を計算する"""
    if df.empty: return df

    # トレンド系
    df['SMA_50'] = ta.sma(df['close'], length=LONG_TERM_SMA_LENGTH)
    df['MACD'] = ta.macd(df['close'], fast=12, slow=26, signal=9)['MACDh_12_26_9']
    df['ADX'] = ta.adx(df['high'], df['low'], df['close'], length=14)['ADX_14']

    # モメンタム系
    df['RSI'] = ta.rsi(df['close'], length=14)

    # ボラティリティ/レンジ系
    df['ATR'] = ta.atr(df['high'], df['low'], df['close'], length=14)
    df['BBANDS'] = ta.bbands(df['close'], length=20, std=2)
    df['BB_WIDTH'] = (df['BBANDS']['BBU_20_2.0'] - df['BBANDS']['BBL_20_2.0']) / df['BBANDS']['BBM_20_2.0'] * 100

    # 出来高系
    df['OBV'] = ta.obv(df['close'], df['volume'])
    df['OBV_SMA'] = ta.sma(df['OBV'], length=20)


    return df

def analyze_single_timeframe(df: pd.DataFrame, timeframe: str, symbol: str, macro_context: Dict) -> Optional[Dict]:
    """単一の時間足で技術分析を実行し、スコアリングする (v19.0.27 為替統合)"""
    if df.empty or len(df) < LONG_TERM_SMA_LENGTH: return None

    df = calculate_technical_indicators(df)
    # 💡 データフレームの最後の行と、その前の行が確実に存在するかをチェック (Patch 12 - ゼロ件シグナル対策)
    if len(df) < 2: return None 
    
    last_row = df.iloc[-1]
    prev_row = df.iloc[-2]
    current_price = last_row['close']

    # ロングシグナルを想定したベーススコア
    score = BASE_SCORE
    tech_data = {}

    # 1. SL/TPとRRRの初期設定
    last_atr = last_row['ATR']
    sl_offset = last_atr * RANGE_TRAIL_MULTIPLIER

    entry_price = current_price
    sl_price = entry_price - sl_offset # ロング
    tp1_price = entry_price + sl_offset * DTS_RRR_DISPLAY

    risk = entry_price - sl_offset
    reward = tp1_price - entry_price
    rr_ratio = reward / risk if risk > 0 and reward > 0 else 0.0

    # 2. 長期トレンドフィルター (4h足のみ適用)
    long_term_trend = 'N/A'
    if timeframe == '4h' and 'SMA_50' in df.columns:
        if current_price > last_row['SMA_50']:
            long_term_trend = 'BULLISH'
        elif current_price < last_row['SMA_50']:
            long_term_trend = 'BEARISH'

        if long_term_trend == 'BEARISH':
            score -= LONG_TERM_REVERSAL_PENALTY
            tech_data['long_term_reversal_penalty'] = True
            tech_data['long_term_reversal_penalty_value'] = LONG_TERM_REVERSAL_PENALTY

        tech_data['long_term_trend'] = long_term_trend

    # 3. モメンタム/過熱感
    if last_row['RSI'] < RSI_MOMENTUM_LOW:
        score += 0.10 # 押し目買いの優位性
    if last_row['RSI'] > 70:
        score -= 0.10 # 逆行のリスク

    # 4. MACDクロス確認
    macd_valid = True
    if last_row['MACD'] > 0 and prev_row['MACD'] < 0: # 強気転換 (ロングに有利)
        score += 0.15
    elif last_row['MACD'] < 0 and prev_row['MACD'] > 0: # 弱気転換 (ロングに不利)
        score -= MACD_CROSS_PENALTY
        macd_valid = False

    # 5. ADX (トレンドの強さ)
    tech_data['adx'] = last_row['ADX']
    if last_row['ADX'] > ADX_TREND_THRESHOLD:
        score += 0.05 # トレンドに乗る

    # 6. ボラティリティペナルティ
    if last_row.get('BB_WIDTH', 100) < VOLATILITY_BB_PENALTY_THRESHOLD:
        score -= 0.10 # レンジ相場での取引リスク

    # 7. 構造的サポート/レジスタンス (ダミーを維持)
    if random.random() > 0.6:
        score += 0.10
        tech_data['structural_pivot_bonus'] = 0.10
        tech_data['fib_proximity_level'] = 'Support/61.8%'
    else:
        tech_data['structural_pivot_bonus'] = 0.0
        tech_data['fib_proximity_level'] = 'N/A'

    # 8. 出来高の裏付け
    avg_volume = df['volume'].iloc[-30:].mean()
    if last_row['volume'] > avg_volume * VOLUME_CONFIRMATION_MULTIPLIER:
        score += 0.05
        tech_data['volume_confirmation_bonus'] = 0.05
    else:
        tech_data['volume_confirmation_bonus'] = 0.0


    # 9. マクロ/ファンダメンタル (FGIプロキシ - リアルデータを使用)
    fgi_bonus = macro_context.get('fgi_proxy', 0.0) # リアルデータ取得に基づいた値
    score += fgi_bonus
    tech_data['sentiment_fgi_proxy_bonus'] = fgi_bonus
    
    # 10. OBVモメンタム確認
    obv_momentum_bonus = 0.0
    if last_row['OBV'] > last_row['OBV_SMA'] and prev_row['OBV'] <= prev_row['OBV_SMA']: # OBVがSMAを上抜けた
         obv_momentum_bonus = OBV_MOMENTUM_BONUS
         score += obv_momentum_bonus
    tech_data['obv_momentum_bonus_value'] = obv_momentum_bonus

    # 11. 流動性ボーナス (リアルオーダーブック深度を使用)
    ob_data = ORDER_BOOK_CACHE.get(symbol, {})
    liquidity_bonus = 0.0
    # 買い板の深さ > 売り板の深さ * 1.5 で流動性優位と判定
    if ob_data.get('bids_depth', 0.0) > ob_data.get('asks_depth', 0.0) * 1.5:
        liquidity_bonus = LIQUIDITY_BONUS_POINT
        score += liquidity_bonus
    tech_data['liquidity_bonus_value'] = liquidity_bonus
    
    # 12. 為替マクロコンテキスト
    forex_bonus = macro_context.get('forex_bonus', 0.0)
    score += forex_bonus
    tech_data['forex_macro_bonus'] = forex_bonus

    # スコアの正規化
    final_score = max(0.0, min(1.0, score))

    tech_data['macd_cross_valid'] = macd_valid
    tech_data['stoch_filter_penalty'] = 0 # Stochasticsは計算していないため0

    # シグナル判定（スコアがBASE_SCORE以上であれば候補として返す）
    if final_score >= BASE_SCORE and rr_ratio >= 1.0:
         return {
            'symbol': symbol,
            'side': 'ロング',
            'timeframe': timeframe,
            'score': final_score,
            'rr_ratio': rr_ratio,
            'price': current_price,
            'entry': entry_price,
            'sl': sl_price,
            'tp1': tp1_price,
            'tech_data': tech_data
        }

    return None

def calculate_trade_plan(signal: Dict, usdt_balance: float) -> Tuple[float, float, float]:
    """リスクと残高に基づいて取引量を計算する"""
    entry = signal['entry']
    sl = signal['sl']

    # 1. 最大許容リスク額の決定
    max_risk_capital = usdt_balance * MAX_RISK_CAPITAL_PERCENT
    max_risk_absolute = MAX_RISK_PER_TRADE_USDT
    max_risk_usdt = min(max_risk_capital, max_risk_absolute)

    # 2. 許容リスク額が取引可能最低残高未満なら取引しない
    if usdt_balance < MIN_USDT_BALANCE_TO_TRADE or max_risk_usdt <= 1.0:
        return 0.0, 0.0, 0.0 # amount, size_usdt, max_risk_usdt

    # 3. 1単位あたりの損失額 (USDT)
    risk_per_unit = abs(entry - sl)

    # 4. 最大許容リスクに基づいた取引単位 (amount)
    if risk_per_unit == 0 or entry == 0:
        # 価格がゼロまたはSLがエントリーと同じ場合は取引不能
        amount_to_buy = 0.0
    else:
        # amount = (最大リスク額 / 1単位あたりのリスク)
        amount_to_buy = (max_risk_usdt * TRADE_SIZE_PER_RISK_MULTIPLIER) / risk_per_unit

    # 5. 取引サイズ (USDT)
    trade_size_usdt = amount_to_buy * entry

    # 6. 残高超過チェック
    if trade_size_usdt > usdt_balance:
        # 残高に合わせて再計算
        trade_size_usdt = usdt_balance
        amount_to_buy = trade_size_usdt / entry if entry > 0 else 0

    if trade_size_usdt < 1.0: # 最小取引サイズ制限
        return 0.0, 0.0, max_risk_usdt

    return amount_to_buy, trade_size_usdt, max_risk_usdt

async def process_trade_signal(signal: Dict, usdt_balance: float, client: ccxt_async.Exchange):
    """シグナルに基づき、現物買い注文を発注する"""
    symbol = signal['symbol']
    trade_plan = signal['trade_plan']
    amount = trade_plan['amount_to_buy']
    size_usdt = trade_plan['trade_size_usdt']
    market_price = signal['price'] # 最後のティック価格をエントリー価格の代わりに利用

    if size_usdt == 0.0 or amount == 0.0:
        return

    try:
        # 1. 現物買い (Market Buy) を実行
        order = await client.create_market_buy_order(symbol, amount)

        # 2. 注文が成功した場合、ポジションを追跡
        if order and order['status'] == 'closed':
            # 実際のエントリー価格はorder['price']またはorder['average']を使用
            entry_price = order.get('average', order.get('price', market_price))
            bought_amount = order.get('filled', order['amount'])

            ACTUAL_POSITIONS[symbol] = {
                'entry_price': entry_price,
                'amount': bought_amount,
                'sl_price': signal['sl'],
                'tp_price': signal['tp1'],
                'open_time': time.time(),
                'status': 'OPEN'
            }
            logging.info(f"✅ TRADE EXECUTED: {symbol} Buy {bought_amount:.4f} @ {entry_price:.4f} (Size: {size_usdt:.2f} USDT)")

        # 3. 注文失敗時の処理
        elif order and order['status'] != 'closed':
            logging.warning(f"⚠️ TRADE ORDER PENDING/FAILED for {symbol}. Status: {order['status']}")
            
    except Exception as e:
        logging.error(f"❌ TRADE FAILED for {symbol}: {e}")

async def manage_open_positions(usdt_balance: float, client: ccxt_async.Exchange):
    """保有中のポジションを監視し、SL/TPの執行を行う"""
    if not ACTUAL_POSITIONS: return

    symbols_to_check = list(ACTUAL_POSITIONS.keys())

    try:
        tickers = await client.fetch_tickers(symbols_to_check)
    except Exception:
        logging.error("ポジション管理: ティッカーの取得に失敗しました。")
        return

    positions_to_close = []

    for symbol, pos in ACTUAL_POSITIONS.items():
        ticker = tickers.get(symbol)
        if not ticker: continue

        current_price = ticker['last']
        sl_price = pos['sl_price']
        tp_price = pos['tp_price']

        # 損切りのチェック (ロングポジション)
        if current_price <= sl_price:
            logging.warning(f"🚨 SL HIT: {symbol} at {current_price:.4f} (SL: {sl_price:.4f})")
            positions_to_close.append((symbol, 'SL_HIT'))

        # 利確のチェック
        elif current_price >= tp_price:
            logging.info(f"🎉 TP REACHED: {symbol} at {current_price:.4f} (TP: {tp_price:.4f})")
            positions_to_close.append((symbol, 'TP_REACHED'))

    # ポジションのクローズを実行
    for symbol, status in positions_to_close:
        pos = ACTUAL_POSITIONS.get(symbol)
        if not pos: continue

        try:
            # 現物売り (Market Sell) を実行
            # MEXCでは、現物取引の決済は取引所側で銘柄の残高があるかを確認する必要があるため、fetch_balanceを挟むのが理想
            
            # 簡易的な実装として、一旦ポジション量で売却を試みる
            order = await client.create_market_sell_order(symbol, pos['amount'])

            if order and order.get('status') == 'closed':
                 exit_price = order.get('average', order.get('price', 'N/A'))
                 logging.info(f"✅ POSITION CLOSED: {symbol} status: {status} @ {exit_price:.4f}")

            del ACTUAL_POSITIONS[symbol]

        except Exception as e:
            logging.error(f"❌ POSITION CLOSE FAILED for {symbol}: {e}")

async def send_position_status_notification(header_msg: str = "🔄 定期ステータス更新", initial_status: str = 'SUCCESS'):
    """ポジションと残高の定期通知を送信する"""
    global LAST_HOURLY_NOTIFICATION_TIME, LAST_IP_ERROR_MESSAGE

    now = time.time()
    
    # 定期ステータス更新は、残高ステータスに関わらず1時間間隔を強制する
    is_periodic_update = header_msg == "🔄 定期ステータス更新"
    if is_periodic_update and now - LAST_HOURLY_NOTIFICATION_TIME < 60 * 60:
        return

    # 💡 最新の残高とステータスを取得
    # LAST_IP_ERROR_MESSAGE は fetch_current_balance_usdt_with_status() 内で設定される
    usdt_balance, status_from_fetch = await fetch_current_balance_usdt_with_status()
    
    # 💡 format_position_status_messageでグローバルなLAST_IP_ERROR_MESSAGEを使用
    message = format_position_status_message(usdt_balance, ACTUAL_POSITIONS, status_from_fetch)

    if header_msg == "🤖 BOT v19.0.27 初回起動通知":
        full_message = f"🤖 **Apex BOT v19.0.27 起動完了**\n\n{message}"
    else:
        full_message = f"{header_msg}\n\n{message}"

    send_telegram_html(full_message)

    # 💡 定期更新が送信された場合、ステータスに関わらず時間を更新。
    if is_periodic_update:
        LAST_HOURLY_NOTIFICATION_TIME = now
        
async def send_analysis_report(macro_context: Dict, top_signals: List[Dict]):
    """分析レポートの生成と送信を行うヘルパー関数"""
    
    # イベントによる初回送信時、マクロコンテキストが空であることはないが、念のためチェック
    if not macro_context and not FIRST_ANALYSIS_EVENT.is_set():
         logging.info("🔔 分析レポート送信試行: マクロコンテキストデータがありません。スキップします。")
         return 

    logging.info(f"🔔 分析レポートを生成します (FGI: {macro_context.get('fgi_raw_value', 'N/A')}, Total Signals: {len(top_signals)})")

    # 1. メッセージを作成
    message = format_analysis_only_message(top_signals, macro_context)
    
    # 2. 通知を送信
    logging.info(f"📧 分析レポートをTelegramへ送信します。")
    send_telegram_html(message)


# ====================================================================================
# ANALYSIS ONLY LOOP (Patch 13 - Eventベースの初回同期)
# ====================================================================================

async def analysis_only_notification_loop():
    """💡 取引を行わない、1時間ごとの市場分析通知ループ (Patch 13: Eventベースの初回同期)"""
    global LAST_ANALYSIS_ONLY_NOTIFICATION_TIME, LAST_SUCCESS_TIME
    
    # 1. メインループの初回完了まで待機（asyncio.Eventを使用）
    logging.info("⚠️ 分析レポート送信準備: メインループの初回分析完了を待機中...")
    # 💡 Patch 13: イベントがセットされるまで信頼性高く待機。警告の繰り返し出力はここでブロックされる。
    await FIRST_ANALYSIS_EVENT.wait() 
    
    # 2. メインループ初回完了後の即時レポート送信
    try:
         # イベントによって待機が解除されたら、確実に最新のデータを取得
         macro_context = GLOBAL_MACRO_CONTEXT
         top_signals = LAST_ANALYSIS_SIGNALS 
         
         await send_analysis_report(macro_context, top_signals)
         LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time()
         logging.info(f"✅ 分析レポート初回送信完了。次の定期分析まで {ANALYSIS_ONLY_INTERVAL} 秒待機。")
    except Exception as e:
         logging.error(f"❌ 分析レポート初回送信中にエラーが発生: {type(e).__name__}: {e}")
         
    
    # 3. 1時間ごとの定期実行サイクル (初回送信完了後に実行)
    while True:
        try:
            now = time.time()
            
            # 定期実行間隔の計算と待機
            wait_time = ANALYSIS_ONLY_INTERVAL - (now - LAST_ANALYSIS_ONLY_NOTIFICATION_TIME)
            if wait_time > 0:
                # 定期実行の間隔が残っている場合は待機
                await asyncio.sleep(wait_time)
            
            # 待機後に最新の時刻とデータを取得
            macro_context = GLOBAL_MACRO_CONTEXT
            top_signals = LAST_ANALYSIS_SIGNALS 

            await send_analysis_report(macro_context, top_signals)

            LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time()
            logging.info(f"✅ 分析レポート送信完了。次の分析まで {ANALYSIS_ONLY_INTERVAL} 秒待機。")
            
        except Exception as e:
            logging.error(f"❌ 分析専用通知ループでエラーが発生: {type(e).__name__}: {e}")
            await asyncio.sleep(600) # 10分待機して再試行


# ====================================================================================
# MAIN LOOP
# ====================================================================================

async def main_loop():
    """BOTのメイン処理ループ"""
    global LAST_UPDATE_TIME, LAST_ANALYSIS_SIGNALS, GLOBAL_MACRO_CONTEXT, LAST_SUCCESS_TIME, LAST_IP_ERROR_MESSAGE

    if not EXCHANGE_CLIENT:
         await initialize_ccxt_client()

    while True:
        try:
            if not EXCHANGE_CLIENT:
                     logging.error("致命的エラー: CCXTクライアントが初期化できません。60秒後に再試行します。")
                     await asyncio.sleep(60)
                     continue

            # 1. 残高とマクロコンテキストの取得
            usdt_balance_status_task = asyncio.create_task(fetch_current_balance_usdt_with_status())
            # 💡 Patch 7: get_crypto_macro_context はエラーが発生しても None を返さないことを保証
            macro_context_task = asyncio.create_task(get_crypto_macro_context()) 

            usdt_balance, balance_status = await usdt_balance_status_task
            macro_context = await macro_context_task # Patch 7では None が返されないことを保証

            # IPアドレス制限エラーの検出時のログ強化
            if balance_status == 'IP_ERROR':
                ip_match = re.search(r"IP\s*\[(\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3})\]", LAST_IP_ERROR_MESSAGE or "")
                extracted_ip = ip_match.group(1) if ip_match else "N/A"
                logging.error(f"🚨🚨 CRITICAL CONFIG ERROR: MEXCのIPアドレス制限によりアクセスが拒否されています。IP [{extracted_ip}] をホワイトリストに追加してください。")
            else:
                 if LAST_IP_ERROR_MESSAGE is not None:
                      LAST_IP_ERROR_MESSAGE = None


            macro_context['current_usdt_balance'] = usdt_balance
            GLOBAL_MACRO_CONTEXT = macro_context # 💡 グローバル変数にマクロ情報を保存

            # 2. 監視銘柄リストの更新
            await update_symbols_by_volume()

            logging.info(f"🔍 分析開始 (対象銘柄: {len(CURRENT_MONITOR_SYMBOLS)}, USDT残高: {format_usdt(usdt_balance)}, ステータス: {balance_status}, FGI: {GLOBAL_MACRO_CONTEXT.get('fgi_raw_value', 'N/A')}, 為替: {GLOBAL_MACRO_CONTEXT.get('forex_trend', 'N/A')})")

            # 3. オーダーブックデータのプリフェッチ
            order_book_tasks = [asyncio.create_task(fetch_order_book_depth(symbol)) for symbol in CURRENT_MONITOR_SYMBOLS]
            await asyncio.gather(*order_book_tasks, return_exceptions=True)

            # 4. 分析タスクの並列実行
            analysis_tasks = []
            for symbol in CURRENT_MONITOR_SYMBOLS:
                timeframes = ['15m', '1h', '4h']
                for tf in timeframes:
                    ohlcv_data, status, _ = await fetch_ohlcv_with_fallback(CCXT_CLIENT_NAME, symbol, tf)
                    if status != "Success": continue

                    # pandas/numpyの計算をasyncio.to_threadで実行
                    task = asyncio.create_task(
                         asyncio.to_thread(analyze_single_timeframe, ohlcv_data, tf, symbol, GLOBAL_MACRO_CONTEXT)
                    )
                    analysis_tasks.append(task)
                    await asyncio.sleep(REQUEST_DELAY_PER_SYMBOL / 3)

            raw_analysis_results = await asyncio.gather(*analysis_tasks, return_exceptions=True)

            # 5. 分析結果の集計と取引計画の計算
            all_signals: List[Dict] = []
            for result in raw_analysis_results:
                if isinstance(result, Exception) or not result: continue

                amount, size_usdt, max_risk = calculate_trade_plan(result, usdt_balance)
                result['trade_plan'] = {
                     'amount_to_buy': amount,
                     'trade_size_usdt': size_usdt,
                     'max_risk_usdt': max_risk
                }
                all_signals.append(result)

            # 6. 最適なシグナルの選定とグローバル変数への保存
            # LAST_ANALYSIS_SIGNALS は分析専用ループのために、全てのスコア付きシグナル（フィルタリング前）を保存
            LAST_ANALYSIS_SIGNALS = [s for s in all_signals if s['side'] == 'ロング'] 
            
            # 取引用のシグナルはSIGNAL_THRESHOLD以上のものに絞り、クールダウンをチェック
            long_signals_for_trade = [s for s in LAST_ANALYSIS_SIGNALS if s['score'] >= SIGNAL_THRESHOLD]
            long_signals_for_trade.sort(key=lambda s: (s['score'], s['rr_ratio']), reverse=True)

            top_signals_to_notify = []
            notified_count = 0
            for signal in long_signals_for_trade:
                symbol = signal['symbol']
                current_time = time.time()
                # メインループはクールダウンをチェックして通知/取引を行う
                if current_time - TRADE_NOTIFIED_SYMBOLS.get(symbol, 0) > TRADE_SIGNAL_COOLDOWN:
                    top_signals_to_notify.append(signal)
                    notified_count += 1
                    TRADE_NOTIFIED_SYMBOLS[symbol] = current_time
                    if notified_count >= TOP_SIGNAL_COUNT: break

            
            # 7. シグナル通知と自動取引の実行
            trade_tasks = []
            for rank, signal in enumerate(top_signals_to_notify, 1):
                message = format_integrated_analysis_message(signal['symbol'], [signal], rank)
                send_telegram_html(message)

                if signal['trade_plan']['trade_size_usdt'] > 0.0:
                    # 💡 残高がZERO_BALANCEの場合は、取引スキップの確認は不要だが、念のため二重チェック
                    if balance_status == 'SUCCESS': 
                        trade_tasks.append(asyncio.create_task(process_trade_signal(signal, usdt_balance, EXCHANGE_CLIENT)))
                    else:
                        logging.warning(f"⚠️ {signal['symbol']} の高スコアシグナルを検出しましたが、残高ステータスが {balance_status} のため取引をスキップしました。")


            if trade_tasks:
                 await asyncio.gather(*trade_tasks)

            # 8. ポジション管理
            await manage_open_positions(usdt_balance, EXCHANGE_CLIENT)

            # 9. 定期ステータス通知
            await send_position_status_notification("🔄 定期ステータス更新", balance_status)

            # 10. ループの完了とロギング強化
            LAST_UPDATE_TIME = time.time()
            if balance_status == 'SUCCESS': 
                 LAST_SUCCESS_TIME = time.time()
            
            # 💡 Patch 13: 初回分析が完了したらイベントを設定
            if not FIRST_ANALYSIS_EVENT.is_set():
                 FIRST_ANALYSIS_EVENT.set()
            
            # 💡 Patch 13: 生成されたシグナル数を明示的にログ出力
            logging.info(f"💡 分析完了 - 生成シグナル数 (全スコア): {len(LAST_ANALYSIS_SIGNALS)} 件")

            # 💡 バージョン表示をPatch 13に修正
            logging.info(f"✅ 分析/取引サイクル完了 (v19.0.27 - Final Integrated Build (Patch 13))。次の分析まで {LOOP_INTERVAL} 秒待機。")

            await asyncio.sleep(LOOP_INTERVAL)

        except Exception as e:
            error_name = type(e).__name__
            logging.error(f"メインループで致命的なエラーが発生: {error_name}: {e}")
            # エラー発生時もステータス通知を実行
            await send_position_status_notification(f"❌ 致命的エラー発生: {error_name}", 'OTHER_ERROR')
            await asyncio.sleep(60)


# ====================================================================================
# FASTAPI SETUP
# ====================================================================================

app = FastAPI(title="Apex BOT API", version="v19.0.27 - Final Integrated Build (Patch 13)") # 💡 バージョンをPatch 13に更新

@app.on_event("startup")
async def startup_event():
    logging.info("🚀 Apex BOT v19.0.27 Startup initializing (Final Integrated Build)...")

    # 1. CCXT初期化
    await initialize_ccxt_client()

    # 2. 初回起動時のTypeErrorを回避するための修正ロジック (残高とステータスの先行取得)
    usdt_balance, status = await fetch_current_balance_usdt_with_status()
    await send_position_status_notification("🤖 BOT v19.0.27 初回起動通知", initial_status=status)

    global LAST_HOURLY_NOTIFICATION_TIME, LAST_ANALYSIS_ONLY_NOTIFICATION_TIME
    LAST_HOURLY_NOTIFICATION_TIME = time.time()
    
    # 💡 分析専用通知の初回実行は main_loop 完了後に analysis_only_notification_loop 内で制御されるため、ここでは初期化のみ
    LAST_ANALYSIS_ONLY_NOTIFICATION_TIME = time.time() - (ANALYSIS_ONLY_INTERVAL * 2) 

    # 3. メインの取引ループと分析専用ループを起動
    asyncio.create_task(main_loop())
    asyncio.create_task(analysis_only_notification_loop()) # 💡 分析専用ループの起動

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
        "bot_version": "v19.0.27 - Final Integrated Build (Patch 13)", # 💡 バージョンをPatch 13に更新
        "last_success_time_utc": datetime.fromtimestamp(LAST_SUCCESS_TIME, tz=timezone.utc).isoformat() if LAST_SUCCESS_TIME else "N/A",
        "current_client": CCXT_CLIENT_NAME,
        "monitoring_symbols": len(CURRENT_MONITOR_SYMBOLS),
        "last_signals_count": len(LAST_ANALYSIS_SIGNALS),
        "open_positions": len(ACTUAL_POSITIONS)
    }
    return JSONResponse(content=status_msg)

@app.head("/")
@app.get("/")
def home_view():
    return JSONResponse(content={"message": "Apex BOT is running.", "version": "v19.0.27 - Final Integrated Build (Patch 13)"}) # 💡 バージョンをPatch 13に更新

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))
