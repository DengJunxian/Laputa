"""Resource locks only; balances live exclusively in PortfolioLedger."""
from civitas.contracts.portfolio import Reservation


class ReservationStore:
    def __init__(self):
        self._records: dict[str, Reservation] = {}
        self._totals: dict[tuple[str,str], int] = {}
        self._commands = {}
        self._orders = {}

    def get(self, reservation_id: str) -> Reservation | None:
        return self._records.get(reservation_id)

    def total(self, account_id: str, resource_id: str) -> int:
        return self._totals.get((account_id,resource_id),0)

    def records(self) -> tuple[Reservation,...]:
        return tuple(self._records.values())

    def replace(self, reservation: Reservation) -> None:
        for mapping,key in ((self._commands,reservation.command.command_id),(self._orders,reservation.command.order_id)):
            if key in mapping and mapping[key]!=reservation.reservation_id:
                raise ValueError('Reservation command/order identity conflict')
        self._commands[reservation.command.command_id]=reservation.reservation_id
        self._orders[reservation.command.order_id]=reservation.reservation_id
        old=self.get(reservation.reservation_id)
        if old:
            key=(old.command.account_id,old.resource_id)
            self._totals[key]-=old.amount
        self._records[reservation.reservation_id]=reservation
        key=(reservation.command.account_id,reservation.resource_id)
        self._totals[key]=self._totals.get(key,0)+reservation.amount
