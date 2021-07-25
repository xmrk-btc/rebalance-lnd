import math

from output import Output, format_alias, format_fee_msat, format_ppm, format_amount, format_boring_string, format_fee_sat, \
    format_warning, format_error, format_earning, format_fee_msat_red
from routes import Routes

DEFAULT_BASE_FEE_SAT_MSAT = 1000
DEFAULT_FEE_RATE_MSAT = 0.001
MAX_FEE_RATE = 2000


class Logic:
    excluded = []

    def __init__(
        self,
        lnd,
        first_hop_channel,
        last_hop_channel,
        amount,
        excluded,
        fee_factor,
        output: Output
    ):
        self.lnd = lnd
        self.first_hop_channel = first_hop_channel
        self.last_hop_channel = last_hop_channel
        self.amount = amount
        if excluded:
            self.excluded = excluded
        self.fee_factor = fee_factor
        self.output = output
        if not self.fee_factor:
            self.fee_factor = 1.0

    def rebalance(self):
        first_hop_alias_formatted = ""
        last_hop_alias_formatted = ""
        first_channel_id = 0
        if self.first_hop_channel:
            first_hop_alias_formatted = format_alias(self.lnd.get_node_alias(self.first_hop_channel.remote_pubkey))
            first_channel_id = format_boring_string(self.first_hop_channel.chan_id)
        if self.last_hop_channel:
            last_hop_alias_formatted = format_alias(self.lnd.get_node_alias(self.last_hop_channel.remote_pubkey))
        amount_formatted = format_amount(self.amount)
        if self.first_hop_channel and self.last_hop_channel:
            self.output.print_line(
                f"Sending {amount_formatted} satoshis from channel {first_channel_id} with {first_hop_alias_formatted} "
                f"back through {last_hop_alias_formatted}."
            )
            self.output.print_line(f"Sending {amount_formatted} satoshis.")
        elif self.last_hop_channel:
            self.output.print_line(f"Sending {amount_formatted} satoshis back through {last_hop_alias_formatted}.")
        else:
            self.output.print_line(
                f"Sending {self.amount:,} satoshis from channel {first_channel_id} with {first_hop_alias_formatted}."
            )

        fee_limit_msat = self.get_fee_limit_msat()
        payment_request = self.generate_invoice()
        min_ppm_last_hop = None
        if self.first_hop_channel:
            policy_first_hop = self.lnd.get_policy_to(self.first_hop_channel.chan_id)
            min_ppm_last_hop = self.fee_factor * policy_first_hop.fee_rate_milli_msat
        routes = Routes(
            self.lnd,
            payment_request,
            self.first_hop_channel,
            self.last_hop_channel,
            fee_limit_msat,
            min_ppm_last_hop,
            self.output
        )

        self.initialize_ignored_channels(routes, fee_limit_msat)

        tried_routes = []
        while routes.has_next():
            route = routes.get_next()

            success = self.try_route(payment_request, route, routes, tried_routes)
            if success:
                return True
        self.output.print_line("Could not find any suitable route")
        return False

    def get_fee_limit_msat(self):
        fee_limit_msat = None
        if self.last_hop_channel:
            policy = self.lnd.get_policy_to(self.last_hop_channel.chan_id)
            fee_rate = policy.fee_rate_milli_msat
            last_hop_alias = self.lnd.get_node_alias(self.last_hop_channel.remote_pubkey)
            if fee_rate > MAX_FEE_RATE:
                self.output.print_line(
                    f"Calculating using capped fee rate {MAX_FEE_RATE} "
                    f"for inbound channel (with {last_hop_alias}, original fee rate {fee_rate})"
                )
                fee_rate = MAX_FEE_RATE
            fee_limit_msat = self.fee_factor * self.compute_fee(
                self.amount * 1_000, fee_rate, policy
            )
            fee_limit_msat = max(1_000, fee_limit_msat)
            fee_limit_formatted = format_fee_msat(int(fee_limit_msat))
            ppm_limit = int(fee_limit_msat / self.amount * 1_000)
            ppm_limit_formatted = format_ppm(ppm_limit)
            self.output.print_without_linebreak(f"Setting fee limit to {fee_limit_formatted} ({ppm_limit_formatted})")
            if self.fee_factor != 1.0:
                self.output.print_line(f" (factor {self.fee_factor}).")
            else:
                self.output.print_line(".")

        return fee_limit_msat

    def try_route(self, payment_request, route, routes, tried_routes):
        if self.route_is_invalid(route, routes):
            return False

        tried_routes.append(route)
        self.output.print_line("")
        self.output.print_without_linebreak(f"Trying route #{len(tried_routes)}")
        fee_msat = route.total_fees_msat
        route_ppm = int(route.total_fees_msat * 1_000_000 / route.total_amt_msat)
        fee_formatted = format_fee_msat(fee_msat)
        ppm_formatted = format_ppm(route_ppm)
        self.output.print_line(f" (fee {fee_formatted}, {ppm_formatted})")
        self.output.print_route(route)

        response = self.lnd.send_payment(payment_request, route)
        is_successful = response.failure.code == 0
        if is_successful:
            last_hop_alias = format_alias(self.lnd.get_node_alias(route.hops[-2].pub_key))
            first_hop_alias = format_alias(self.lnd.get_node_alias(route.hops[0].pub_key))
            first_hop_ppm = self.lnd.get_policy_to(route.hops[0].chan_id).fee_rate_milli_msat
            last_hop_ppm = self.lnd.get_policy_to(route.hops[-1].chan_id).fee_rate_milli_msat
            first_hop_ppm_formatted = format_ppm(first_hop_ppm)
            last_hop_ppm_formatted = format_ppm(last_hop_ppm)
            self.output.print_line("")
            sats_formatted = format_fee_sat(int(route.hops[-1].amt_to_forward))
            self.output.print_line(
                f"Increased outbound liquidity on {last_hop_alias} ({last_hop_ppm_formatted}) by "
                f"{sats_formatted}"
            )
            self.output.print_line(f"Increased inbound liquidity on {first_hop_alias} "
                                   f"({first_hop_ppm_formatted} configured for outbound)")
            fees_formatted = format_fee_msat(route.total_fees_msat)
            ppm_formatted = format_ppm(route_ppm)
            self.output.print_line(f"Fee: {route.total_fees:,} sats ({fees_formatted}, {ppm_formatted})")
            self.output.print_line("")
            self.output.print_line("Successful route:")
            self.output.print_route(route)
            return True
        else:
            self.handle_error(response, route, routes)
            return False

    def handle_error(self, response, route, routes):
        code = response.failure.code
        failure_source_pubkey = Logic.get_failure_source_pubkey(response, route)
        if code == 15:
            self.output.print_line(format_warning("Temporary channel failure"))
            routes.ignore_edge_on_route(failure_source_pubkey, route)
        elif code == 18:
            self.output.print_line(format_warning("Unknown next peer"))
            routes.ignore_edge_on_route(failure_source_pubkey, route)
        elif code == 12:
            self.output.print_line(format_warning("Fee insufficient"))
            routes.ignore_edge_on_route(failure_source_pubkey, route)
        elif code == 14:
            self.output.print_line(format_warning("Channel disabled"))
            routes.ignore_edge_on_route(failure_source_pubkey, route)
        else:
            self.output.print_line(format_error(f"Unknown error code {repr(code)}:"))
            self.output.print_line(format_error(repr(response)))

    @staticmethod
    def get_failure_source_pubkey(response, route):
        if response.failure.failure_source_index == 0:
            failure_source_pubkey = route.hops[-1].pub_key
        else:
            failure_source_pubkey = route.hops[
                response.failure.failure_source_index - 1
            ].pub_key
        return failure_source_pubkey

    def route_is_invalid(self, route, routes):
        first_hop = route.hops[0]
        last_hop = route.hops[-1]
        if self.low_local_ratio_after_sending(first_hop, route.total_amt):
            self.output.print_without_linebreak("Outbound channel would have low local ratio after sending, ")
            routes.ignore_first_hop(self.get_channel_for_channel_id(first_hop.chan_id))
            return True
        if self.first_hop_and_last_hop_use_same_channel(first_hop, last_hop):
            self.output.print_without_linebreak("Outbound and inbound channel are identical, ")
            hop_before_last_hop = route.hops[-2]
            routes.ignore_edge_from_to(
                last_hop.chan_id, hop_before_last_hop.pub_key, last_hop.pub_key
            )
            return True
        if self.high_local_ratio_after_receiving(last_hop):
            self.output.print_without_linebreak(
                "Inbound channel would have high local ratio after receiving, "
            )
            hop_before_last_hop = route.hops[-2]
            routes.ignore_edge_from_to(
                last_hop.chan_id, hop_before_last_hop.pub_key, last_hop.pub_key
            )
            return True
        return self.fees_too_high(route, routes)

    def low_local_ratio_after_sending(self, first_hop, total_amount):
        if self.first_hop_channel:
            # Just use the computed/specified amount to drain the first hop, ignoring fees
            return False
        channel_id = first_hop.chan_id
        channel = self.get_channel_for_channel_id(channel_id)
        if channel is None:
            self.output.print_line(f"Unable to get channel information for hop {repr(first_hop)}")
            return True

        remote = channel.remote_balance + total_amount
        local = channel.local_balance - total_amount
        ratio = float(local) / (remote + local)
        return ratio < 0.5

    def high_local_ratio_after_receiving(self, last_hop):
        if self.last_hop_channel:
            return False
        channel_id = last_hop.chan_id
        channel = self.get_channel_for_channel_id(channel_id)
        if channel is None:
            self.output.print_line(f"Unable to get channel information for hop {repr(last_hop)}")
            return True

        amount = last_hop.amt_to_forward
        remote = channel.remote_balance - amount
        local = channel.local_balance + amount
        ratio = float(local) / (remote + local)
        return ratio > 0.5

    @staticmethod
    def first_hop_and_last_hop_use_same_channel(first_hop, last_hop):
        return first_hop.chan_id == last_hop.chan_id

    def fees_too_high(self, route, routes):
        policy_first_hop = self.lnd.get_policy_to(route.hops[0].chan_id)
        amount_msat = route.total_amt_msat
        missed_fee_msat = self.compute_fee(
            amount_msat, policy_first_hop.fee_rate_milli_msat, policy_first_hop
        )
        policy_last_hop = self.lnd.get_policy_to(route.hops[-1].chan_id)
        fee_rate_last_hop = policy_last_hop.fee_rate_milli_msat
        original_fee_rate_last_hop = fee_rate_last_hop
        if fee_rate_last_hop > MAX_FEE_RATE:
            fee_rate_last_hop = MAX_FEE_RATE
        expected_income_msat = self.fee_factor * self.compute_fee(
            amount_msat, fee_rate_last_hop, policy_last_hop
        )
        rebalance_fee_msat = route.total_fees_msat
        high_fees = rebalance_fee_msat + missed_fee_msat > expected_income_msat
        if high_fees:
            difference_msat = -rebalance_fee_msat - missed_fee_msat + expected_income_msat
            first_hop_alias = format_alias(self.lnd.get_node_alias(route.hops[0].pub_key))
            last_hop_alias = format_alias(self.lnd.get_node_alias(route.hops[-2].pub_key))
            self.output.print_line("")
            if fee_rate_last_hop != original_fee_rate_last_hop:
                self.output.print_line(
                    f"Calculating using capped fee rate {MAX_FEE_RATE} for inbound channel "
                    f"(with {last_hop_alias}, original fee rate {original_fee_rate_last_hop})"
                )
            route_ppm = int(route.total_fees_msat * 1_000_000 / route.total_amt_msat)
            route_fee_formatted = format_fee_msat(rebalance_fee_msat)
            route_ppm_formatted = format_ppm(route_ppm)
            self.output.print_line(
                f"Skipping route due to high fees "
                f"(fee {route_fee_formatted}, {route_ppm_formatted})"
            )
            self.output.print_route(route)
            future_income_formatted = format_earning(math.floor(expected_income_msat), 8)
            self.output.print_without_linebreak(
                f"  {future_income_formatted}: "
                f"expected future fee income for inbound channel (with {last_hop_alias})"
            )
            if self.fee_factor != 1.0:
                self.output.print_line(f" (factor {self.fee_factor})")
            else:
                self.output.print_line("")
            transaction_fees_formatted = format_fee_msat(int(rebalance_fee_msat), 8)
            missed_fee_formatted = format_fee_msat(math.ceil(missed_fee_msat), 8)
            difference_formatted = format_fee_msat_red(math.ceil(difference_msat), 8)
            self.output.print_line(f"- {transaction_fees_formatted}: rebalance transaction fees")
            self.output.print_line(f"- {missed_fee_formatted}: "
                                   f"missing out on future fees for outbound channel (with {first_hop_alias})")
            self.output.print_line(f"= {difference_formatted}")
            routes.ignore_high_fee_hops(route)
        return high_fees

    @staticmethod
    def compute_fee(amount, fee_rate, policy):
        expected_fee_msat = amount / 1000000 * fee_rate + policy.fee_base_msat / 1000
        return expected_fee_msat

    def generate_invoice(self):
        if self.last_hop_channel:
            memo = f"Rebalance of channel with ID {self.last_hop_channel.chan_id}"
        else:
            memo = f"Rebalance of channel with ID {self.first_hop_channel.chan_id}"
        return self.lnd.generate_invoice(memo, self.amount)

    def get_channel_for_channel_id(self, channel_id):
        for channel in self.lnd.get_channels():
            if channel.chan_id == channel_id:
                if not hasattr(channel, "local_balance"):
                    channel.local_balance = 0
                if not hasattr(channel, "remote_balance"):
                    channel.remote_balance = 0
                return channel
        self.output.print_line(f"Unable to find channel with id {channel_id}!")

    def initialize_ignored_channels(self, routes, fee_limit_msat):
        if self.first_hop_channel:
            # avoid me - X - me via the same channel/peer
            chan_id = self.first_hop_channel.chan_id
            from_pub_key = self.first_hop_channel.remote_pubkey
            to_pub_key = self.lnd.get_own_pubkey()
            routes.ignore_edge_from_to(
                chan_id, from_pub_key, to_pub_key, show_message=False
            )
            if not self.last_hop_channel:
                self.ignore_last_hops_with_high_ratio(routes)
        if self.last_hop_channel:
            # avoid me - X - me via the same channel/peer
            chan_id = self.last_hop_channel.chan_id
            from_pub_key = self.lnd.get_own_pubkey()
            to_pub_key = self.last_hop_channel.remote_pubkey
            routes.ignore_edge_from_to(
                chan_id, from_pub_key, to_pub_key, show_message=False
            )
            self.ignore_first_hops_with_fee_rate_higher_than_last_hop(routes)
        if self.last_hop_channel and fee_limit_msat:
            # ignore first hops with high fee rate configured by our node (causing high missed future fees)
            max_fee_rate_first_hop = math.ceil(fee_limit_msat * 1_000 / self.amount)
            for channel in self.lnd.get_channels():
                policy = self.lnd.get_policy_to(channel.chan_id)
                fee_rate = policy.fee_rate_milli_msat
                if fee_rate > max_fee_rate_first_hop and self.first_hop_channel != channel:
                    routes.ignore_first_hop(channel, show_message=False)
        for channel in self.lnd.get_channels():
            if self.low_local_ratio_after_sending(channel, self.amount):
                routes.ignore_first_hop(channel, show_message=False)
            if channel.chan_id in self.excluded:
                self.output.print_without_linebreak("Channel is excluded, ")
                routes.ignore_first_hop(channel)

    def ignore_first_hops_with_fee_rate_higher_than_last_hop(self, routes):
        policy_last_hop = self.lnd.get_policy_to(self.last_hop_channel.chan_id)
        last_hop_fee_rate = policy_last_hop.fee_rate_milli_msat
        from_pub_key = self.lnd.get_own_pubkey()
        for channel in self.lnd.get_channels():
            chan_id = channel.chan_id
            policy = self.lnd.get_policy_to(chan_id)
            if policy.fee_rate_milli_msat > last_hop_fee_rate:
                to_pub_key = channel.remote_pubkey
                routes.ignore_edge_from_to(
                    chan_id, from_pub_key, to_pub_key, show_message=False
                )

    def ignore_last_hops_with_high_ratio(self, routes):
        for channel in self.lnd.get_channels():
            channel_id = channel.chan_id
            if channel is None:
                return
            remote = channel.remote_balance - self.amount
            local = channel.local_balance + self.amount
            ratio = float(local) / (remote + local)
            if ratio > 0.5:
                to_pub_key = self.lnd.get_own_pubkey()
                routes.ignore_edge_from_to(
                    channel_id, channel.remote_pubkey, to_pub_key, show_message=False
                )
