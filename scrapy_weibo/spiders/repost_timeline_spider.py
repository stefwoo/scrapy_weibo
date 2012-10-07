import redis
import time
import math
import simplejson as json
from scrapy.spider import BaseSpider
from scrapy_weibo.items import WeiboItem, UserItem
from scrapy import log
from scrapy.conf import settings
from scrapy.exceptions import CloseSpider
from scrapy.http import Request


REDIS_HOST = 'localhost'
REDIS_PORT = 6379
WEIBOIDS_SET = '{spider}:weiboids'
BASE_URL = 'https://api.weibo.com/2/statuses/repost_timeline.json?id={id}&page={page}&count=200'
SOURCE_WEIBO_URL = 'https://api.weibo.com/2/statuses/show.json?id={id}'


class RepostTimelineSpider(BaseSpider):
    name = 'repost_timeline'
    r = None

    def start_requests(self):
        wids = self.prepare()

        for wid in wids:
            request = Request(SOURCE_WEIBO_URL.format(id=wid), headers=None,
                              callback=self.soucre_weibo)
            request.meta['retry'] = 0
            request.meta['wid'] = wid

            yield request

    def soucre_weibo(self, response):
        retry = response.meta['retry']
        wid = response.meta['wid']
        resp = json.loads(response.body)
        if "error_code" in resp and resp["error_code"] in [21314, 21315, 21316, 21317]:
            raise CloseSpider('ERROR: TOKEN NOT VALID')

        try:
            user, weibo, retweeted_user = self.resp_to_item(resp)
        except KeyError:
            retry += 1
            if retry > 3:
                return

            request = Request(SOURCE_WEIBO_URL.format(id=wid), headers=None,
                              callback=self.soucre_weibo, dont_filter=True)

            request.meta['retry'] = retry
            request.meta['wid'] = wid
            yield request

        yield user
        yield weibo
        if retweeted_user is not None:
            yield retweeted_user

        reposts_count = weibo['reposts_count']
        wid = weibo['id']
        for i in range(1, int(math.ceil(reposts_count / 200.0)) + 1):
            request = Request(BASE_URL.format(id=wid, page=i), headers=None,
                              callback=self.more_reposts)

            request.meta['page'] = i
            request.meta['retry'] = 0
            request.meta['wid'] = wid
            request.meta['source_weibo'] = weibo

            yield request

    def resp_to_item(self, resp):
        """ /statuses/show """

        weibo = WeiboItem()
        user = UserItem()

        weibo_keys = ['created_at', 'id', 'mid', 'text', 'source', 'reposts_count',
                      'comments_count', 'attitudes_count', 'geo']
        for k in weibo_keys:
            try:
                weibo[k] = resp[k]
            except KeyError, e:
                raise KeyError(e)

        weibo['timestamp'] = self.local2unix(weibo['created_at'])

        weibo['reposts'] = []

        user_keys = ['id', 'name', 'gender', 'province', 'city', 'location',
                     'description', 'verified', 'followers_count',
                     'statuses_count', 'friends_count', 'profile_image_url',
                     'bi_followers_count', 'verified']
        for k in user_keys:
            try:
                user[k] = resp['user'][k]
            except KeyError, e:
                raise KeyError(e)

        weibo['user'] = user

        retweeted_user = None
        if 'retweeted_status' in resp and 'deleted' not in resp['retweeted_status']:
            retweeted_status = WeiboItem()
            retweeted_user = UserItem()

            for k in weibo_keys:
                retweeted_status[k] = resp['retweeted_status'][k]
            retweeted_status['timestamp'] = self.local2unix(retweeted_status['created_at'])

            for k in user_keys:
                retweeted_user[k] = resp['retweeted_status']['user'][k]

            retweeted_status['user'] = retweeted_user
            weibo['retweeted_status'] = retweeted_status

        return user, weibo, retweeted_user

    def local2unix(self, time_str):
        time_format = '%a %b %d %H:%M:%S +0800 %Y'
        return time.mktime(time.strptime(time_str, time_format))

    def more_reposts(self, response):
        resp = json.loads(response.body)
        page = response.meta['page']
        retry = response.meta['retry']
        wid = response.meta['wid']
        source_weibo = response.meta['source_weibo']

        if "error_code" in resp and resp["error_code"] in [21314, 21315, 21316, 21317]:
            raise CloseSpider('ERROR: TOKEN NOT VALID')

        if 'reposts' in resp and resp['reposts'] == []:
            retry += 1
            if retry > 3:
                return
            request = Request(BASE_URL.format(id=wid, page=page), headers=None,
                              callback=self.more_reposts, dont_filter=True)

            request.meta['page'] = page
            request.meta['retry'] = retry
            request.meta['wid'] = wid
            request.meta['source_weibo'] = source_weibo

            yield request
            return

        for repost in resp['reposts']:
            try:
                user, weibo, retweeted_user = self.resp_to_item(repost)
            except KeyError:
                continue
            source_weibo['reposts'].append(weibo)
            yield user
            yield weibo
            if retweeted_user is not None:
                yield retweeted_user

        yield source_weibo

    def prepare(self):
        host = settings.get("REDIS_HOST", REDIS_HOST)
        port = settings.get("REDIS_PORT", REDIS_PORT)
        weiboids_set = WEIBOIDS_SET.format(spider=self.name)

        self.r = redis.Redis(host, port)

        log.msg("load weiboids from {weiboids_set}".format(weiboids_set=weiboids_set), level=log.INFO)
        weiboids = self.r.smembers(weiboids_set)
        if len(weiboids) == 0:
            log.msg("{spider}: NO WEIBO IDS TO LOAD".format(spider=self.name), level=log.WARNING)

        return weiboids
