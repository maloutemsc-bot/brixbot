#!/usr/bin/env python
# by Dominik Stanisław Suchora <hexderm@gmail.com>
# License: GNU GPLv3

import os
import sys
import re
import json
from typing import Callable, Generator
from datetime import datetime
from pathlib import Path
import argparse

# from curl_cffi import requests
import requests
from reliq import RQ
import treerequests

reliq = RQ(cached=True)


def valid_directory(directory: str):
    if os.path.isdir(directory):
        return directory
    else:
        raise argparse.ArgumentTypeError('"{}" is not a directory'.format(directory))


class rule34xxx:
    def __init__(self, domain="https://rule34.xxx", **kwargs):
        self.domain = domain
        self.ses = treerequests.Session(
            requests,
            requests.Session,
            lambda x, y: treerequests.reliq(x, y, obj=reliq),
            **kwargs,
        )

    def go_through_pages(self, url: str, func: Callable) -> Generator:
        nexturl = url
        page = 1
        while True:
            paged = func(nexturl, page)
            nexturl = paged["nexturl"]

            yield paged

            if nexturl is None or len(nexturl) == 0:
                break
            page += 1

    @staticmethod
    def conv_date(date):
        if len(date) == 0:
            return date

        return datetime.strptime(date, "%Y-%m-%d %H:%M:%S").isoformat()

    def get_comments(self, rq, comments=True):
        ret = []
        while True:
            r = rq.json(
                r"""
                .comments div #comment-list; div #b>c child@; {
                    div .col1; {
                        [0] a child@; {
                            .user_link.U * self@ | "%(href)Dv" trim,
                            .user * self@ | "%Di" trim
                        },
                        .id.u span child@ | "%i",
                        b child@; {
                            .date * self@ | "%Dt" tr "\n\t\r" sed "s/.*Posted on //; s/Score:.*//" trim,
                            .score.u a id=b>s | "%i",
                        }
                    },
                    .text div .col2 | "%i" tr "\n\t\r" sed "s/<br *\/?>/\n/g" "E" decode trim
                } |
            """
            )
            ret += r["comments"]
            if not comments:
                break

            nexturl = rq.json(
                r""" .u.U div #paginator; a alt=next | "%(onclick)v" sed "s/.*location='//; s/'.*//" decode trim """
            )["u"]
            if len(nexturl) == 0:
                break
            rq = self.ses.get_html(nexturl)

        for i in ret:
            i["date"] = self.conv_date(i["date"])

        return ret

    def post_url(self, p_id):
        return self.domain + "/index.php?page=post&s=view&id={}".format(p_id)

    @staticmethod
    def post_id(url):
        return int(re.search(r"&id=(\d+)", url)[1])

    def get_post(self, url, p_id=0, comments=True):
        if p_id != 0:
            url = self.post_url(p_id)

        rq, resp = self.ses.get_html(url, response=True)

        r = rq.json(
            r"""
            .image.U img #image src | "%(src)Dv" trim,
            .original.U div .link-list; a i@"Original image" | "%(href)Dv" trim,
            div #stats; {
                .id.u li i@b>"Id: " | "%i" sed "s/^.*: //",
                li i@"Posted: "; {
                    .date * self@  | "%i" sed "s/^[A-Za-z0-9]*: //; s/<.*//;q" trim,
                    * self@; [0] a; {
                        .uploader_link.U * self@ | "%(href)Dv" trim,
                        .uploader * self@  | "%Di" sed "/ /!p" "n" trim
                    }
                },
                .rating li i@"Rating: " | "%Di" / sed 's/.*: //' trim,
                .sources.a.U li i@"Source:"; a | "%(href)v\n",
               .sizex.u li i@b>"Size: " | "%i" sed "s/^.*: //; s/x.*//",
               .sizey.u li i@b>"Size: " | "%i" sed "s/^.*: //; s/.*x//",
               .score.u span #B>"psc[0-9]*" | "%i"
            },
            ul #tag-sidebar; {
                .copyright.a li .tag-type-copyright .tag; a [-] | "%Di\n" / trim "\n",
                .character.a li .tag-type-character .tag; a [-] | "%Di\n" / trim "\n",
                .artist.a li .tag-type-artist .tag; a [-] | "%Di\n" / trim "\n",
                .general.a li .tag-type-general .tag; a [-] | "%Di\n" / trim "\n",
                .metadata.a li .tag-type-metadata .tag; a [-] | "%Di\n" / trim "\n",
            },
            .comments_count.u div #comment-list | "%t",
            """
        )
        r["url"] = url
        r["comments"] = self.get_comments(rq, comments=comments)

        r["date"] = self.conv_date(r["date"])

        return r, resp.status_code

    def save_post(self, workdir, url, p_id=0, comments=True):
        if p_id == 0:
            p_id = self.post_id(url)

        fname = Path(workdir) / str(p_id)

        nonexisiting_path = str(fname) + "_e"

        if os.path.exists(nonexisiting_path):
            return
        if fname.exists() and os.path.getsize(fname) > 10:
            return

        try:
            r, code = self.get_post(url=url, p_id=p_id, comments=comments)
        except requests.RequestException:
            print("{} failed".format(p_id))
            return

        if code >= 300 and code < 400:
            with open(nonexisiting_path, "w") as f:
                f.write("\n")
            return

        with open(fname, "w") as f:
            json.dump(r, f, separators=(",", ":"))
            f.write("\n")

    def save_posts(self, workdir, firstid=1, lastid=0, comments=True):
        if lastid == 0:
            r = self.get_lastpost_id()
            lastid = min(lastid, r) if lastid != 0 else r

        for i in range(firstid, lastid + 1):
            self.save_post(workdir, "", p_id=i, comments=comments)

    def get_lastpost_id(self):
        rq = self.ses.get_html(self.domain + "/index.php?page=post&s=list&tags=all")

        return rq.json(r'.u.u span .thumb; [0] a href | "%(href)v\n" sed "s/.*=//"')[
            "u"
        ]

    def get_page_posts(self, rq):
        return rq.json(r'.urls.a.U div .image-list; a id | "%(href)v\n"')["urls"]

    @staticmethod
    def post_url_to_id(url):
        r = re.search(r"[?&]id=(\d+)", url)
        if r is None:
            return 0
        return int(r[1])

    def get_page(self, url: str, page: int = 1) -> dict:
        rq = self.ses.get_html(url)

        nexturl = rq.json(r'.u.U div #paginator; [0] a alt=next | "%(href)Dv" trim')[
            "u"
        ]

        lastpage = rq.json(
            r'.u.u div #paginator; [0] a alt="last page" | "%(href)v" / sed "s/.*(\?|&|&amp;)pid=//;s/( |\?|&|&amp;).*//" "E"'
        )["u"]

        return {
            "url": url,
            "nexturl": nexturl,
            "page": page,
            "lastpage": lastpage,
            "posts": self.get_page_posts(rq),
        }

    def get_pages(self):
        return self.go_through_pages(
            "https://rule34.xxx/index.php?page=post&s=list&tags=all", self.get_page
        )


def argparser():
    parser = argparse.ArgumentParser(
        description="Tool for getting things from rule34. If no URLs provided scrapes the whole site",
        add_help=False,
    )

    parser.add_argument(
        "urls",
        metavar="URL",
        type=str,
        nargs="*",
        help="urls",
    )

    parser.add_argument(
        "-h",
        "--help",
        action="help",
        help="Show this help message and exit",
    )
    parser.add_argument(
        "-d",
        "--directory",
        metavar="DIR",
        type=valid_directory,
        help="Use DIR as working directory",
        default=".",
    )
    parser.add_argument(
        "-D",
        "--domain",
        metavar="DOMAIN",
        type=str,
        help="set DOMAIN, by default set to https://rule34.xxx",
        default="https://rule34.xxx",
    )
    parser.add_argument(
        "--no-comments",
        action="store_true",
        help="don't make requests to get comments",
    )
    parser.add_argument(
        "-f",
        "--first",
        metavar="ID",
        type=int,
        help="get posts, starting from ID",
        default=1,
    )
    parser.add_argument(
        "-l",
        "--last",
        metavar="ID",
        type=int,
        help="get posts, ending at ID",
        default=0,
    )
    parser.add_argument(
        "--last-id",
        action="store_true",
        help="print id of the latest post and exit",
    )

    treerequests.args_section(parser)

    return parser


def cli(argv: list[str]):
    args = argparser().parse_args(argv)

    net_settings = {"logger": treerequests.simple_logger(sys.stdout)}

    rl34 = rule34xxx(args.domain, **net_settings)
    treerequests.args_session(rl34.ses, args)

    if args.last_id:
        rl34.ses["logger"] = None
        print(rl34.get_lastpost_id())
        return

    for i in args.urls:
        rl34.save_post(args.directory, i, comments=not args.no_comments)

    if len(args.urls) == 0:
        rl34.save_posts(
            args.directory,
            lastid=args.last,
            firstid=args.first,
            comments=not args.no_comments,
        )


if __name__ == "__main__":
    cli(sys.argv[1:] if sys.argv[1:] else ["-h"])
