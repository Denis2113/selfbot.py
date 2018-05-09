import discord
from discord.ext import commands
from cogs.utils.dataIO import dataIO
from collections import namedtuple, defaultdict, deque
from datetime import datetime
from copy import deepcopy
from .utils import checks
from cogs.utils.chat_formatting import pagify, box
from enum import Enum
from __main__ import send_cmd_help
import os
import time
import logging
import random

default_settings = {"PAYDAY_TIME": 300, "PAYDAY_CREDITS": 120,
                    "SLOT_MIN": 5, "SLOT_MAX": 100000, "SLOT_TIME": 0,
                    "REGISTER_CREDITS": 500}


class EconomyError(Exception):
    pass


class OnCooldown(EconomyError):
    pass


class InvalidBid(EconomyError):
    pass


class BankError(Exception):
    pass


class AccountAlreadyExists(BankError):
    pass


class NoAccount(BankError):
    pass


class InsufficientBalance(BankError):
    pass


class NegativeValue(BankError):
    pass


class SameSenderAndReceiver(BankError):
    pass


NUM_ENC = "\N{COMBINING ENCLOSING KEYCAP}"


class SMReel(Enum):
    cherries  = "\N{CHERRIES}"
    cookie    = "\N{COOKIE}"
    two       = "\N{DIGIT TWO}" + NUM_ENC
    flc       = "\N{FOUR LEAF CLOVER}"
    cyclone   = "\N{CYCLONE}"
    sunflower = "\N{SUNFLOWER}"
    six       = "\N{DIGIT SIX}" + NUM_ENC
    mushroom  = "\N{MUSHROOM}"
    heart     = "\N{HEAVY BLACK HEART}"
    snowflake = "\N{SNOWFLAKE}"

PAYOUTS = {
    (SMReel.two, SMReel.two, SMReel.six) : {
        "payout" : lambda x: x * 2500 + x,
        "phrase" : "JACKPOT! 226! Bani tai au fost Multiplicati * 2500!"
    },
    (SMReel.flc, SMReel.flc, SMReel.flc) : {
        "payout" : lambda x: x + 1000,
        "phrase" : "4LC! +1000!"
    },
    (SMReel.cherries, SMReel.cherries, SMReel.cherries) : {
        "payout" : lambda x: x + 800,
        "phrase" : "3 cirese! +800!"
    },
    (SMReel.two, SMReel.six) : {
        "payout" : lambda x: x * 4 + x,
        "phrase" : "2 6! Bani tai au fost multiplicati * 4!"
    },
    (SMReel.cherries, SMReel.cherries) : {
        "payout" : lambda x: x * 3 + x,
        "phrase" : "2 cirese! Bani tau au fost multiplicati * 3!"
    },
    "3 symbols" : {
        "payout" : lambda x: x + 500,
        "phrase" : "3 SEMNE! +500!"
    },
    "2 symbols" : {
        "payout" : lambda x: x * 2 + x,
        "phrase" : "2 semne la fel, bani multiplicati * 2!"
    },
}

SLOT_PAYOUTS_MSG = ("Slot machine payouts:\n"
                    "{two.value} {two.value} {six.value} Bet * 2500\n"
                    "{flc.value} {flc.value} {flc.value} +1000\n"
                    "{cherries.value} {cherries.value} {cherries.value} +800\n"
                    "{two.value} {six.value} Bet * 4\n"
                    "{cherries.value} {cherries.value} Bet * 3\n\n"
                    "Three symbols: +500\n"
                    "Two symbols: Bet * 2".format(**SMReel.__dict__))


class Bank:

    def __init__(self, bot, file_path):
        self.accounts = dataIO.load_json(file_path)
        self.bot = bot

    def create_account(self, user, *, initial_balance=0):
        server = user.server
        if not self.account_exists(user):
            if server.id not in self.accounts:
                self.accounts[server.id] = {}
            if user.id in self.accounts:  # Legacy account
                balance = self.accounts[user.id]["balance"]
            else:
                balance = initial_balance
            timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            account = {"name": user.name,
                       "balance": balance,
                       "created_at": timestamp
                       }
            self.accounts[server.id][user.id] = account
            self._save_bank()
            return self.get_account(user)
        else:
            raise AccountAlreadyExists()

    def account_exists(self, user):
        try:
            self._get_account(user)
        except NoAccount:
            return False
        return True

    def withdraw_credits(self, user, amount):
        server = user.server

        if amount < 0:
            raise NegativeValue()

        account = self._get_account(user)
        if account["balance"] >= amount:
            account["balance"] -= amount
            self.accounts[server.id][user.id] = account
            self._save_bank()
        else:
            raise InsufficientBalance()

    def deposit_credits(self, user, amount):
        server = user.server
        if amount < 0:
            raise NegativeValue()
        account = self._get_account(user)
        account["balance"] += amount
        self.accounts[server.id][user.id] = account
        self._save_bank()

    def set_credits(self, user, amount):
        server = user.server
        if amount < 0:
            raise NegativeValue()
        account = self._get_account(user)
        account["balance"] = amount
        self.accounts[server.id][user.id] = account
        self._save_bank()

    def transfer_credits(self, sender, receiver, amount):
        if amount < 0:
            raise NegativeValue()
        if sender is receiver:
            raise SameSenderAndReceiver()
        if self.account_exists(sender) and self.account_exists(receiver):
            sender_acc = self._get_account(sender)
            if sender_acc["balance"] < amount:
                raise InsufficientBalance()
            self.withdraw_credits(sender, amount)
            self.deposit_credits(receiver, amount)
        else:
            raise NoAccount()

    def can_spend(self, user, amount):
        account = self._get_account(user)
        if account["balance"] >= amount:
            return True
        else:
            return False

    def wipe_bank(self, server):
        self.accounts[server.id] = {}
        self._save_bank()

    def get_server_accounts(self, server):
        if server.id in self.accounts:
            raw_server_accounts = deepcopy(self.accounts[server.id])
            accounts = []
            for k, v in raw_server_accounts.items():
                v["id"] = k
                v["server"] = server
                acc = self._create_account_obj(v)
                accounts.append(acc)
            return accounts
        else:
            return []

    def get_all_accounts(self):
        accounts = []
        for server_id, v in self.accounts.items():
            server = self.bot.get_server(server_id)
            if server is None:
                # Servers that have since been left will be ignored
                # Same for users_id from the old bank format
                continue
            raw_server_accounts = deepcopy(self.accounts[server.id])
            for k, v in raw_server_accounts.items():
                v["id"] = k
                v["server"] = server
                acc = self._create_account_obj(v)
                accounts.append(acc)
        return accounts

    def get_balance(self, user):
        account = self._get_account(user)
        return account["balance"]

    def get_account(self, user):
        acc = self._get_account(user)
        acc["id"] = user.id
        acc["server"] = user.server
        return self._create_account_obj(acc)

    def _create_account_obj(self, account):
        account["member"] = account["server"].get_member(account["id"])
        account["created_at"] = datetime.strptime(account["created_at"],
                                                  "%Y-%m-%d %H:%M:%S")
        Account = namedtuple("Account", "id name balance "
                             "created_at server member")
        return Account(**account)

    def _save_bank(self):
        dataIO.save_json("data/economy/bank.json", self.accounts)

    def _get_account(self, user):
        server = user.server
        try:
            return deepcopy(self.accounts[server.id][user.id])
        except KeyError:
            raise NoAccount()


class SetParser:
    def __init__(self, argument):
        allowed = ("+", "-")
        if argument and argument[0] in allowed:
            try:
                self.sum = int(argument)
            except:
                raise
            if self.sum < 0:
                self.operation = "withdraw"
            elif self.sum > 0:
                self.operation = "deposit"
            else:
                raise
            self.sum = abs(self.sum)
        elif argument.isdigit():
            self.sum = int(argument)
            self.operation = "set"
        else:
            raise


class Economy:
    """Economy

    Get rich and have fun with imaginary currency!"""

    def __init__(self, bot):
        global default_settings
        self.bot = bot
        self.bank = Bank(bot, "data/economy/bank.json")
        self.file_path = "data/economy/settings.json"
        self.settings = dataIO.load_json(self.file_path)
        if "PAYDAY_TIME" in self.settings:  # old format
            default_settings = self.settings
            self.settings = {}
        self.settings = defaultdict(default_settings.copy, self.settings)
        self.payday_register = defaultdict(dict)
        self.slot_register = defaultdict(dict)

    @commands.group(name="bank", pass_context=True)
    async def _bank(self, ctx):
        """Bank operations"""
        if ctx.invoked_subcommand is None:
            await send_cmd_help(ctx)

    @_bank.command(pass_context=True, no_pm=True)
    async def register(self, ctx):
        """Registers an account at the Twentysix bank"""
        settings = self.settings[ctx.message.server.id]
        author = ctx.message.author
        credits = 0
        if ctx.message.server.id in self.settings:
            credits = settings.get("REGISTER_CREDITS", 0)
        try:
            account = self.bank.create_account(author, initial_balance=credits)
            await self.bot.say("{} Contu e pornit, Bani: {}"
                               "".format(author.mention, account.balance))
        except AccountAlreadyExists:
            await self.bot.say("{} Deja ai un cont aici "
                               " nab.".format(author.mention))

    @_bank.command(pass_context=True)
    async def balance(self, ctx, user: discord.Member=None):
        """Shows balance of user.

        Defaults to yours."""
        if not user:
            user = ctx.message.author
            try:
                await self.bot.say("{} Valoarea ta e de: {}".format(
                    user.mention, self.bank.get_balance(user)))
            except NoAccount:
                await self.bot.say("{} Tu nu ai un cont la "
                                   " Banca de Tauri. Scrie `{}bank register`"
                                   " Sa-ti faci un cont.".format(user.mention,
                                                          ctx.prefix))
        else:
            try:
                await self.bot.say("{}'s Ai {} bani!".format(
                    user.name, self.bank.get_balance(user)))
            except NoAccount:
                await self.bot.say("Membru acela nu are un cont la banca.")

    @_bank.command(pass_context=True)
    async def transfer(self, ctx, user: discord.Member, sum: int):
        """Transferi Bani la alt utilizator"""
        author = ctx.message.author
        try:
            self.bank.transfer_credits(author, user, sum)
            logger.info("{}({}) a transferat {} credite, la {}({})".format(
                author.name, author.id, sum, user.name, user.id))
            await self.bot.say("{} credite au fost transferate la {}'s"
                               " account.".format(sum, user.name))
        except NegativeValue:
            await self.bot.say("Trebuie sa transferi o suma mai mare de 1 banut.")
        except SameSenderAndReceiver:
            await self.bot.say("Nu-ti poti da singur credite.")
        except InsufficientBalance:
            await self.bot.say("N-ai atatia bani HOTULE.")
        except NoAccount:
            await self.bot.say("Ala nu are cont la banca.")

    @_bank.command(name="set", pass_context=True)
    @checks.admin_or_permissions(manage_server=True)
    async def _set(self, ctx, user: discord.Member, credits: SetParser):
        """Sets credits of user's bank account. See help for more operations

        Passing positive and negative values will add/remove credits instead

        Examples:
            bank set @Twentysix 26 - Sets 26 credits
            bank set @Twentysix +2 - Adds 2 credits
            bank set @Twentysix -6 - Removes 6 credits"""
        author = ctx.message.author
        try:
            if credits.operation == "deposit":
                self.bank.deposit_credits(user, credits.sum)
                logger.info("{}({}) added {} credits to {} ({})".format(
                    author.name, author.id, credits.sum, user.name, user.id))
                await self.bot.say("{} banuti au fost adaugati lui {}"
                                   "".format(credits.sum, user.name))
            elif credits.operation == "withdraw":
                self.bank.withdraw_credits(user, credits.sum)
                logger.info("{}({}) au fost scosi {} banuti la {} ({})".format(
                    author.name, author.id, credits.sum, user.name, user.id))
                await self.bot.say("{} bani au fost scosi de la {}"
                                   "".format(credits.sum, user.name))
            elif credits.operation == "set":
                self.bank.set_credits(user, credits.sum)
                logger.info("{}({}) a setat {} bani la {} ({})"
                            "".format(author.name, author.id, credits.sum,
                                      user.name, user.id))
                await self.bot.say("{}'s are amu {}".format(
                    user.name, credits.sum))
        except InsufficientBalance:
            await self.bot.say("NU are destui bani.")
        except NoAccount:
            await self.bot.say("Nu are cont la banca.")

    @_bank.command(pass_context=True, no_pm=True)
    @checks.serverowner_or_permissions(administrator=True)
    async def reset(self, ctx, confirmation: bool=False):
        """Deletes all server's bank accounts"""
        if confirmation is False:
            await self.bot.say("Asta sterje bani la toti di pa sv "
                               "SPUNE NU {}bank reset no, sau da =( "
                               "{}bank reset yes".format(ctx.prefix))
        else:
            self.bank.wipe_bank(ctx.message.server)
            await self.bot.say("Amu toti is saraci "
                               "Esti nesi.")

    @commands.command(pass_context=True, no_pm=True)
    async def payday(self, ctx):  # TODO
        """NI ma credite gratis"""
        author = ctx.message.author
        server = author.server
        id = author.id
        if self.bank.account_exists(author):
            if id in self.payday_register[server.id]:
                seconds = abs(self.payday_register[server.id][
                              id] - int(time.perf_counter()))
                if seconds >= self.settings[server.id]["PAYDAY_TIME"]:
                    self.bank.deposit_credits(author, self.settings[
                                              server.id]["PAYDAY_CREDITS"])
                    self.payday_register[server.id][
                        id] = int(time.perf_counter())
                    await self.bot.say(
                        "{} NA MA SARACULE CREDITE (+{}"
                        " credits!)".format(
                            author.mention,
                            str(self.settings[server.id]["PAYDAY_CREDITS"])))
                else:
                    dtime = self.display_time(
                        self.settings[server.id]["PAYDAY_TIME"] - seconds)
                    await self.bot.say(
                        "{} Nu vrei mai multe?"
                        " asteapta {}.".format(author.mention, dtime))
            else:
                self.payday_register[server.id][id] = int(time.perf_counter())
                self.bank.deposit_credits(author, self.settings[
                                          server.id]["PAYDAY_CREDITS"])
                await self.bot.say(
                    "{} Na ma saracutule (+{} credits!)".format(
                        author.mention,
                        str(self.settings[server.id]["PAYDAY_CREDITS"])))
        else:
            await self.bot.say("{} Iti trebuie un cont la banca sa poti sa iei credite"
                               " Scrie  `{}bank register` sa-ti faci unul.".format(
                                   author.mention, ctx.prefix))

    @commands.group(pass_context=True)
    async def leaderboard(self, ctx):
        """Server / global leaderboard

        Defaults to server"""
        if ctx.invoked_subcommand is None:
            await ctx.invoke(self._server_leaderboard)

    @leaderboard.command(name="server", pass_context=True)
    async def _server_leaderboard(self, ctx, top: int=10):
        """Prints out the server's leaderboard

        Defaults to top 10"""
        # Originally coded by Airenkun - edited by irdumb
        server = ctx.message.server
        if top < 1:
            top = 10
        bank_sorted = sorted(self.bank.get_server_accounts(server),
                             key=lambda x: x.balance, reverse=True)
        bank_sorted = [a for a in bank_sorted if a.member] #  exclude users who left
        if len(bank_sorted) < top:
            top = len(bank_sorted)
        topten = bank_sorted[:top]
        highscore = ""
        place = 1
        for acc in topten:
            highscore += str(place).ljust(len(str(top)) + 1)
            highscore += (str(acc.member.display_name) + " ").ljust(23 - len(str(acc.balance)))
            highscore += str(acc.balance) + "\n"
            place += 1
        if highscore != "":
            for page in pagify(highscore, shorten_by=12):
                await self.bot.say(box(page, lang="py"))
        else:
            await self.bot.say("There are no accounts in the bank.")

    @leaderboard.command(name="global")
    async def _global_leaderboard(self, top: int=10):
        """Prints out the global leaderboard

        Defaults to top 10"""
        if top < 1:
            top = 10
        bank_sorted = sorted(self.bank.get_all_accounts(),
                             key=lambda x: x.balance, reverse=True)
        bank_sorted = [a for a in bank_sorted if a.member] #  exclude users who left
        unique_accounts = []
        for acc in bank_sorted:
            if not self.already_in_list(unique_accounts, acc):
                unique_accounts.append(acc)
        if len(unique_accounts) < top:
            top = len(unique_accounts)
        topten = unique_accounts[:top]
        highscore = ""
        place = 1
        for acc in topten:
            highscore += str(place).ljust(len(str(top)) + 1)
            highscore += ("{} |{}| ".format(acc.member, acc.server)
                          ).ljust(23 - len(str(acc.balance)))
            highscore += str(acc.balance) + "\n"
            place += 1
        if highscore != "":
            for page in pagify(highscore, shorten_by=12):
                await self.bot.say(box(page, lang="py"))
        else:
            await self.bot.say("There are no accounts in the bank.")

    def already_in_list(self, accounts, user):
        for acc in accounts:
            if user.id == acc.id:
                return True
        return False

    @commands.command()
    async def payouts(self):
        """Shows slot machine payouts"""
        await self.bot.whisper(SLOT_PAYOUTS_MSG)

    @commands.command(pass_context=True, no_pm=True)
    async def slot(self, ctx, bid: int):
        """Joaca la pacanele"""
        author = ctx.message.author
        server = author.server
        settings = self.settings[server.id]
        valid_bid = settings["SLOT_MIN"] <= bid and bid <= settings["SLOT_MAX"]
        slot_time = settings["SLOT_TIME"]
        last_slot = self.slot_register.get(author.id)
        now = datetime.utcnow()
        try:
            if last_slot:
                if (now - last_slot).seconds < slot_time:
                    raise OnCooldown()
            if not valid_bid:
                raise InvalidBid()
            if not self.bank.can_spend(author, bid):
                raise InsufficientBalance
            await self.slot_machine(author, bid)
        except NoAccount:
            await self.bot.say("{} Iti trebuie un cont pentru pacanele "
                               " Scrie`{}bank register` sa faci unul."
                               "".format(author.mention, ctx.prefix))
        except InsufficientBalance:
            await self.bot.say("{} Iti trebuie destui bani in cont sa te "
                               "Joci la pacanele.".format(author.mention))
        except OnCooldown:
            await self.bot.say("Ho ma Nabule! Asteapta: {} "
                               "dupa fiecare tura".format(slot_time))
        except InvalidBid:
            await self.bot.say("Bani trebuie sa fie intre {} si {}."
                               "".format(settings["SLOT_MIN"],
                                         settings["SLOT_MAX"]))

    async def slot_machine(self, author, bid):
        default_reel = deque(SMReel)
        reels = []
        self.slot_register[author.id] = datetime.utcnow()
        for i in range(3):
            default_reel.rotate(random.randint(-999, 999)) # weeeeee
            new_reel = deque(default_reel, maxlen=3) # we need only 3 symbols
            reels.append(new_reel)                   # for each reel
        rows = ((reels[0][0], reels[1][0], reels[2][0]),
                (reels[0][1], reels[1][1], reels[2][1]),
                (reels[0][2], reels[1][2], reels[2][2]))

        slot = "~~\n~~" # Mobile friendly
        for i, row in enumerate(rows): # Let's build the slot to show
            sign = "  "
            if i == 1:
                sign = ">"
            slot += "{}{} {} {}\n".format(sign, *[c.value for c in row])

        payout = PAYOUTS.get(rows[1])
        if not payout:
            # Checks for two-consecutive-symbols special rewards
            payout = PAYOUTS.get((rows[1][0], rows[1][1]),
                     PAYOUTS.get((rows[1][1], rows[1][2]))
                                )
        if not payout:
            # Still nothing. Let's check for 3 generic same symbols
            # or 2 consecutive symbols
            has_three = rows[1][0] == rows[1][1] == rows[1][2]
            has_two = (rows[1][0] == rows[1][1]) or (rows[1][1] == rows[1][2])
            if has_three:
                payout = PAYOUTS["3 symbols"]
            elif has_two:
                payout = PAYOUTS["2 symbols"]

        if payout:
            then = self.bank.get_balance(author)
            pay = payout["payout"](bid)
            now = then - bid + pay
            self.bank.set_credits(author, now)
            await self.bot.say("{}\n{} {}\n\nYour bid: {}\n{} → {}!"
                               "".format(slot, author.mention,
                                         payout["phrase"], bid, then, now))
        else:
            then = self.bank.get_balance(author)
            self.bank.withdraw_credits(author, bid)
            now = then - bid
            await self.bot.say("{}\n{} NIMIC!\nYour bid: {}\n{} → {}!"
                               "".format(slot, author.mention, bid, then, now))

    @commands.group(pass_context=True, no_pm=True)
    @checks.admin_or_permissions(manage_server=True)
    async def economyset(self, ctx):
        """Schimbi valoarea economiei"""
        server = ctx.message.server
        settings = self.settings[server.id]
        if ctx.invoked_subcommand is None:
            msg = "```"
            for k, v in settings.items():
                msg += "{}: {}\n".format(k, v)
            msg += "```"
            await send_cmd_help(ctx)
            await self.bot.say(msg)

    @economyset.command(pass_context=True)
    async def slotmin(self, ctx, bid: int):
        """Minimu de bani de bagat"""
        server = ctx.message.server
        self.settings[server.id]["SLOT_MIN"] = bid
        await self.bot.say("Minimul amu este de {} banuti.".format(bid))
        dataIO.save_json(self.file_path, self.settings)

    @economyset.command(pass_context=True)
    async def slotmax(self, ctx, bid: int):
        """Maximul este de"""
        server = ctx.message.server
        self.settings[server.id]["SLOT_MAX"] = bid
        await self.bot.say("Amu poti baga pana la {} banuti.".format(bid))
        dataIO.save_json(self.file_path, self.settings)

    @economyset.command(pass_context=True)
    async def slottime(self, ctx, seconds: int):
        """Secunde pana poti baga mai mult"""
        server = ctx.message.server
        self.settings[server.id]["SLOT_TIME"] = seconds
        await self.bot.say("Cooldown-ul e amu {} secunde.".format(seconds))
        dataIO.save_json(self.file_path, self.settings)

    @economyset.command(pass_context=True)
    async def paydaytime(self, ctx, seconds: int):
        """Secunde pana la toate payday-urile"""
        server = ctx.message.server
        self.settings[server.id]["PAYDAY_TIME"] = seconds
        await self.bot.say("Macar  {} secunde sa treaca"
                           "intre fiecare payday.".format(seconds))
        dataIO.save_json(self.file_path, self.settings)

    @economyset.command(pass_context=True)
    async def paydaycredits(self, ctx, credits: int):
        """Credite la fiecare payday"""
        server = ctx.message.server
        self.settings[server.id]["PAYDAY_CREDITS"] = credits
        await self.bot.say("un payday va da {} banuti."
                           "".format(credits))
        dataIO.save_json(self.file_path, self.settings)

    @economyset.command(pass_context=True)
    async def registercredits(self, ctx, credits: int):
        """Bani cand te inregistrezi"""
        server = ctx.message.server
        if credits < 0:
            credits = 0
        self.settings[server.id]["REGISTER_CREDITS"] = credits
        await self.bot.say("amu cand iti faci cont primesti {} banuti."
                           "".format(credits))
        dataIO.save_json(self.file_path, self.settings)

    # What would I ever do without stackoverflow?
    def display_time(self, seconds, granularity=2):
        intervals = (  # Source: http://stackoverflow.com/a/24542445
            ('weeks', 604800),  # 60 * 60 * 24 * 7
            ('days', 86400),    # 60 * 60 * 24
            ('hours', 3600),    # 60 * 60
            ('minutes', 60),
            ('seconds', 1),
        )

        result = []

        for name, count in intervals:
            value = seconds // count
            if value:
                seconds -= value * count
                if value == 1:
                    name = name.rstrip('s')
                result.append("{} {}".format(value, name))
        return ', '.join(result[:granularity])


def check_folders():
    if not os.path.exists("data/economy"):
        print("Creating data/economy folder...")
        os.makedirs("data/economy")


def check_files():

    f = "data/economy/settings.json"
    if not dataIO.is_valid_json(f):
        print("Creating default economy's settings.json...")
        dataIO.save_json(f, {})

    f = "data/economy/bank.json"
    if not dataIO.is_valid_json(f):
        print("Creating empty bank.json...")
        dataIO.save_json(f, {})


def setup(bot):
    global logger
    check_folders()
    check_files()
    logger = logging.getLogger("red.economy")
    if logger.level == 0:
        # Prevents the logger from being loaded again in case of module reload
        logger.setLevel(logging.INFO)
        handler = logging.FileHandler(
            filename='data/economy/economy.log', encoding='utf-8', mode='a')
        handler.setFormatter(logging.Formatter(
            '%(asctime)s %(message)s', datefmt="[%d/%m/%Y %H:%M]"))
        logger.addHandler(handler)
    bot.add_cog(Economy(bot))
