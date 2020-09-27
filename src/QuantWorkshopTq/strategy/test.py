# -*- coding: utf-8 -*-

__author__ = 'Bruce Frank Wong'


"""
本策略是用来验证是否可以仅靠 <tqsdk.objs.Order> 处理委托单。
"""


from typing import Dict, List, Optional
import time
import datetime

from tqsdk import TqApi, BacktestFinished
from tqsdk.objs import Order, Trade
from tqsdk.entity import Entity
from tqsdk.tafunc import time_to_datetime
from sqlalchemy.orm.exc import NoResultFound

from . import (
    StrategyBase,
    StrategyParameter,
    db_session,
    BacktestOrder,
    BacktestTrade
)


__all__ = 'strategy_parameter', 'TestStrategy'


parameter: List[str] = [
    'max_position',         # 最大持仓手数
    'close_spread',         # 平仓价差
    'order_range',          # 挂单范围
    'closeout_long',        # 多单强平点差
    'closeout_short',       # 空单强平点差
    'volume_per_order',     # 每笔委托手数
    'volume_per_price',     # 每价位手数
]
strategy_parameter = StrategyParameter(parameter)


def is_trading_time(t: datetime.time, tt_list: list) -> bool:
    for tt in tt_list:
        if tt['open'] <= t <= tt['close']:
            return True
    return False


def lots_at_price(order: Entity, p: float) -> int:
    order_id: str
    order: Order
    lots: int = 0
    for order_id, order in order.items():
        if order.limit_price == p:
            lots += order.volume_left
    return lots


class TestStrategy(StrategyBase):
    _strategy_name: str = 'TestStrategy'

    price_ask: float
    price_bid: float

    def __init__(self, api: TqApi, settings: StrategyParameter):
        super().__init__(api=api, symbol='DCE.c2101', settings=settings)

        self.trading_time: List[Dict[str, datetime.time]] = [
            {
                'open': datetime.time(hour=21, minute=0, second=0),
                'close': datetime.time(hour=23, minute=0, second=0),
            },
            {
                'open': datetime.time(hour=9, minute=0, second=0),
                'close': datetime.time(hour=11, minute=30, second=0),
            },
            {
                'open': datetime.time(hour=13, minute=30, second=0),
                'close': datetime.time(hour=15, minute=0, second=0),
            },
        ]

        self.price_ask = 0.0
        self.price_bid = 0.0

    def is_trading_time(self, t: datetime.datetime) -> bool:
        trading_time: Dict[str, datetime.time]
        for trading_time in self.trading_time:
            if trading_time['open'] <= t.time() <= trading_time['close']:
                return True
        return False

    def is_about_to_close(self, t: datetime.datetime) -> bool:
        """
        是否快要闭市。
        :param t: 当前日期时间。
        :return:
        """
        trading_time: Dict[str, datetime.time]
        seconds_current: int = t.hour * 3600 + t.minute * 60 + t.second
        seconds_close: int
        for trading_time in self.trading_time:
            seconds_close = trading_time['close'].hour * 3600 + trading_time['close'].minute * 60
            if trading_time['open'] < t.time() < trading_time['close'] and seconds_close - seconds_current < 120:
                return True
        return False

    @staticmethod
    def db_add_order(order: Order, opponent_order_id: Optional[str] = None):
        if opponent_order_id:
            db_order = db_session.query(BacktestOrder).filter_by(order_id=opponent_order_id).one()
            if db_order.opponent:
                raise ValueError(f'Order with id <{opponent_order_id}> already has opponent order.')
            else:
                db_order.opponent = order.order_id
        new_order: BacktestOrder = BacktestOrder(insert_datetime=datetime.datetime.now(),
                                                 order_id=order.order_id,
                                                 direction=order.direction,
                                                 offset=order.offset,
                                                 price=order.limit_price,
                                                 volume_orign=order.volume_orign,
                                                 status='ALIVE',
                                                 opponent=opponent_order_id
                                                 )
        db_session.add(new_order)
        db_session.commit()

    @staticmethod
    def get_unfilled_order() -> List[Order]:
        return db_session.query(BacktestOrder).filter_by(status='ALIVE').all()

    @staticmethod
    def get_trade(trade_id: str) -> Optional[BacktestTrade]:
        try:
            return db_session.query(BacktestTrade).filter_by(trade_id=trade_id).one()
        except NoResultFound:
            return None

    def handle_orders(self, order: Order):
        """
        处理委托单回报。
        根据 Order.status， Order.volume_orign 和 Order.volume_left 判断:
            1, volume_left = 0
                全部成交
            2, 0 < volume_left < volume_orign
                2A, status = FINISHED
                    部分撤单
                2B, status = ALIVE
                    部分成交
            3, volume_left = volume_orign
                3A, status = FINISHED
                    全部撤单
                3B, status = ALIVE
                    报单
        :param order:
        :return:
        """
        db_order: BacktestOrder
        db_trade: BacktestTrade
        new_status: str

        if order.volume_left == 0:
            if order.status == 'FINISHED':
                new_status = '全部成交'
            else:
                raise RuntimeError('不能理解的委托单状态: order.volume_left = 0 and order.status = ALIVE')
        elif 0 < order.volume_left < order.volume_orign:
            if order.status == 'FINISHED':
                new_status = '部分撤单'
            else:
                new_status = '部分成交'
        else:
            if order.status == 'FINISHED':
                new_status = '全部撤单'
            else:
                new_status = '报单'

        if new_status == '报单':
            try:
                # 存在报单回报信息滞后的情况。所以还不能触发异常。
                db_session.query(BacktestOrder).filter_by(order_id=order.order_id).one()
                # if db_order:
                #     raise RuntimeError('新报单不应该在数据库中有记录，但是在数据库中查到了。')
            except NoResultFound:
                db_session.add(
                    BacktestOrder(
                        insert_datetime=time_to_datetime(order.insert_date_time),
                        order_id=order.order_id,
                        direction=order.direction,
                        offset=order.offset,
                        price=order.limit_price,
                        volume_orign=order.volume_orign,
                        volume_left=order.volume_orign,
                        status='ALIVE'
                    )
                )
                db_session.commit()

            self.log_accept(order)

        if new_status == '全部成交' or new_status == '部分成交':
            # 修正 order 状态
            try:
                db_order = db_session.query(BacktestOrder).filter_by(order_id=order.order_id).one()
                db_order.status = new_status
                db_order.last_datetime = self.remote_datetime
                db_order.volume_left = order.volume_left
                db_session.commit()
            except NoResultFound:
                # 有报单即成交的可能

                db_session.add(
                    BacktestOrder(
                        insert_datetime=time_to_datetime(order.insert_date_time),
                        last_datetime=self.remote_datetime,
                        order_id=order.order_id,
                        direction=order.direction,
                        offset=order.offset,
                        price=order.limit_price,
                        volume_orign=order.volume_orign,
                        volume_left=order.volume_left,
                        status=new_status
                    )
                )
                db_session.commit()
                db_order = db_session.query(BacktestOrder).filter_by(order_id=order.order_id).one()

                self.log_accept(order)

            # 查询 trade
            for _, trade in order.trade_records.items():
                # 新 trade
                if not self.get_trade(trade.trade_id):
                    try:
                        db_trade = db_session.query(BacktestTrade).filter_by(trade_id=trade.trade_id).one()
                        if db_trade:
                            raise RuntimeError('新成交记录不应该在数据库中有记录，但是在数据库中查到了。')
                    except NoResultFound:
                        db_session.add(
                            BacktestTrade(
                                backtest_order_id=db_order.id,
                                order_id=order.order_id,
                                trade_id=trade.trade_id,
                                datetime=time_to_datetime(trade.trade_date_time),
                                exchange_trade_id=trade.exchange_trade_id,
                                exchange_id=trade.exchange_id,
                                instrument_id=trade.instrument_id,
                                direction=trade.direction,
                                offset=trade.offset,
                                price=trade.price,
                                volume=trade.volume
                            )
                        )
                        db_session.commit()

                    self.log_fill(order, trade.trade_id)

        if new_status == '全部撤单' or new_status == '部分撤单':
            try:
                db_order = db_session.query(BacktestOrder).filter_by(order_id=order.order_id).one()
                db_order.status = new_status
                db_order.last_datetime = self.remote_datetime
                db_order.volume_left = order.volume_left
                db_session.commit()
            except NoResultFound:
                print('ERROR in 撤单', order.order_id, '未在数据库中找到')
                self.api.close()
                exit()

            self.log_cancel(order)

    def is_open_condition(self) -> bool:
        """
        开仓条件。

        1、总持仓（多仓 + 空仓）手数 < 【策略】最大持仓手数；
        2、买一价挂单手数 ＋　卖一价挂单手数　＋　每笔委托手数　<　【策略】每价位手数
        3、根据 均线？MACD？判断多空。
        :return:
        """
        max_position = self._settings['max_position']
        position_long = self.tq_position.pos_long
        position_short = self.tq_position.pos_short
        total_position = position_long + position_short

        lots_at_bid = lots_at_price(self.tq_order, self.price_bid)
        lots_at_ask = lots_at_price(self.tq_order, self.price_ask)
        volume_per_order = self._settings['volume_per_order']
        volume_per_price = self._settings['volume_per_price']

        is_position_available = True if total_position < max_position else False
        is_lots_available = True if lots_at_bid + lots_at_ask + volume_per_order < volume_per_price else False

        return is_lots_available and is_position_available

    def run(self):
        # 局部变量
        open_order: Order       # 开仓委托单
        close_order: Order      # 平仓委托单
        remote_order: Order     # 经天勤从服务器发来的委托单
        remote_order_id: str
        remote_trade: Trade     # 成交记录，来自远端
        remote_trade_id: str

        try:
            while True:
                if not self.api.wait_update(deadline=time.time() + self.timeout):
                    print('未在超时限制内接收到数据。')

                if self.api.is_changing(self.tq_quote, ['ask_price1', 'bid_price1']):
                    # tq_quote 中的信息
                    self.price_ask = self.tq_quote.ask_price1  # 当前卖一价
                    self.price_bid = self.tq_quote.bid_price1  # 当前买一价
                    self.remote_datetime = time_to_datetime(self.tq_quote.datetime)  # 当前 datetime

                    # 非交易时间
                    if not self.is_trading_time(self.remote_datetime):
                        self.logger.info(f'{self.remote_datetime}, 【状态】, ——非交易时间')
                        continue

                    # log 当前状态
                    self.log_status()

                    # 处理委托单回报
                    if self.api.is_changing(self.tq_order):
                        for remote_order_id, remote_order in self.tq_order.items():
                            self.handle_orders(remote_order)

                    # 开仓

                    if self.is_open_condition():
                        order_open = self.api.insert_order(symbol=self.symbol,
                                                           direction='BUY',
                                                           offset='OPEN',
                                                           volume=self._settings['volume_per_order'],
                                                           limit_price=self.price_bid
                                                           )
                        self.log_order(order_open)

        except BacktestFinished:
            self.api.close()
            exit()
