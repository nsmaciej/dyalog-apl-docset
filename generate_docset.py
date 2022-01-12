#!/usr/bin/env python3
import itertools
import json
import re
import requests
import shutil
import sqlite3
import subprocess
import sys
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator

import json5
from bs4 import BeautifulSoup
from bs4.element import Comment
from tqdm import trange, tqdm

# Make sure to keep these updated for new versions of Dyalog. Both of these are
# used to patch and run the hlp.js to get better symbol help.
CURRENT_VERSION = "18.0"
HLP_JS_URL = "https://raw.githubusercontent.com/Dyalog/ride/2882e1441c39657a84ab4e6ba3aa932b0b719f33/src/hlp.js"

BASE_URL = "https://help.dyalog.com/latest"
DOCSET_DIR = Path("Dyalog APL.docset")
RESOURCES_DIR = DOCSET_DIR / "Contents" / "Resources"
DOCUMENTS_DIR = RESOURCES_DIR / "Documents"
TMP_DIR = Path("tmp")

ENTRY_TYPES = {
    # Functions.
    "Language/I Beam Functions": "Function",
    "Language/Primitive Functions": "Function",
    "Language/System Functions": "Function",
    # Guides.
    "DotNet": "Guide",
    "InterfaceGuide": "Guide",
    "Language/APL Component Files": "Guide",
    "Language/Appendices/PCRE": "Guide",
    "Language/Defined Functions and Operators": "Guide",
    "Language/Introduction": "Guide",
    "Language/Object Oriented Programming": "Guide",
    "RelNotes": "Guide",
    "UNIX_IUG": "Guide",
    "UserGuide": "Guide",
    "GUI/Examples": "Guide",
    "Language/Error Trapping": "Guide",
    # Sections.
    "MiscPages": "Section",
    "GUI/Miscellaneous": "Section",
    "GUI/SummaryTables": "Section",
    # Objects.
    "GUI/Objects": "Object",
    # These are all sub-pages of various objects.
    "GUI/ChildLists": "Object",
    "GUI/EventLists": "Object",
    "GUI/MethodLists": "Object",
    "GUI/MethodOrEventApplies": "Object",
    "GUI/ParentLists": "Object",
    "GUI/PropLists": "Object",
    "GUI/PropertyApplies": "Object",
    # Other.
    "GUI/Properties": "Property",
    "Language/Control Structures": "Statement",
    "Language/Errors": "Error",
    "Language/Primitive Operators": "Operator",
    "Language/System Commands": "Command",
    # This is basically only for the RIDE help.
    "Language/Symbols": "Notation",
}


def download_jsonp(path: str) -> Any:
    """
    Download and parse a jsonp file.
    """
    url = f"{BASE_URL}{path}"
    r = requests.get(url)
    r.raise_for_status()
    jsonp = re.search(r"define\((.*)\)", r.text)[1]
    return json5.loads(jsonp)


def download_document(path: str) -> Path:
    """
    Download a document into the tmp directory if necessary.
    """
    tmp_path = TMP_DIR / Path(path).relative_to("/")
    if not tmp_path.exists():
        tmp_path.parent.mkdir(parents=True, exist_ok=True)
        url = f"{BASE_URL}{path}"
        r = requests.get(url)
        r.raise_for_status()
        with open(tmp_path, "wb") as fd:
            for chunk in r.iter_content(chunk_size=128):
                fd.write(chunk)
    return tmp_path


def scrape_ride_help() -> dict[str, str]:
    """
    Get the symbol to page mapping used for RIDE F1-help functionality in the
    most hacky way you can imagine. This makes it possible to use APL symbols in
    Dash.  We could carefully scrape a few select pages to get the symbols
    instead but this takes care of figuring out what to scrape.
    """
    path = TMP_DIR / "hlp.js"
    if not path.exists():
        r = requests.get(HLP_JS_URL)
        r.raise_for_status()
        patched = (
            "D={aboutDetails: () => ''}\n"
            + r.text
            + f"D.InitHelp('{CURRENT_VERSION}')\n;console.log(JSON.stringify(D.hlp));"
        )
        path.write_text(patched)
    raw_help = json.loads(subprocess.check_output(["node", str(path)]))
    # Filter out all the stuff that doesn't lead to the docs.
    r = {}
    for title, url in raw_help.items():
        if "#" not in url:
            continue
        r[title] = "/Content/" + url.split("#")[1]
    return r


def scrape_help_toc() -> set[str]:
    """
    Get the pages exposed in the help.dyalog.com Table of Contents.
    """
    # Download Table of Contents tree.
    if not (TMP_DIR / "toc.json").exists():
        toc = download_jsonp("/Data/Tocs/Dyalog.js")
        with open(TMP_DIR / "toc.json", "w") as fd:
            json.dump(toc, fd)

    # Download Table of Contents chunks.
    if not (TMP_DIR / "chunks.json").exists():
        chunks = [
            download_jsonp(f"/Data/Tocs/{toc['prefix']}{i}.js")
            for i in trange(7, desc="Downloading chunks")
        ]
        with open(TMP_DIR / "chunks.json", "w") as fd:
            json.dump(chunks, fd)

    # Extract pages to download.
    with open(TMP_DIR / "chunks.json") as fd:
        chunks = json.load(fd)
    pages = {k for x in chunks for k in x.keys()}
    pages.remove("___")  # This lists topics with no pages.
    return pages


def get_entry_type(path: str, title: str) -> str:
    """
    Get the Dash entry type given a path, handling a few special cases.
    """
    if "GUI/MethodOrEvents" in path:
        return "Event" if " Event" in title else "Method"
    if "UserGuide/Installation and Configuration/Configuration Parameters" in path:
        return "Setting"
    # Crashes if no entry type is found.
    return next(v for k, v in ENTRY_TYPES.items() if k in path)


def is_relative_href(href: str) -> bool:
    return (
        href
        and not urllib.parse.urlparse(href).netloc
        and not href.startswith("javascript:")
        and not href.startswith("mailto:")
    )


def sanitize_html(soup: BeautifulSoup) -> None:
    """
    Process the html to make it ready for Dash.
    """
    # Remove the "Open topic with navigation" link and breadcrumbs.
    for el in soup(class_=["MCWebHelpFramesetLinkTop", "breadcrumbs"]):
            el.extract()

    # Remove all script tags.
    del soup.body["onload"]
    for script in soup("script"):
        script.extract()

    # Patch all relative links to point to new .html pages (instead of .htm).
    for link in soup("a", href=is_relative_href):
        link["href"] = link["href"].replace(".htm", ".html")

    # Add Dash anchors (removing consecutive duplicates). Use get_text(), since
    # string returns None if there are any elements in the heading.
    sections = soup(lambda x: x.name == "h4" and "Example" not in x.get("class", ""))
    if len(sections) >= 2:
        for section in sections:
            heading = re.sub(r" +", " ", str(section.get_text()))
            # Use safe="" to make sure a slash can't appear in the name.
            anchor_name = urllib.parse.quote(heading, safe="")
            anchor = f"<a name='//apple_ref/cpp/Section/{anchor_name}' class='dashAnchor'></a>"
            section.insert_before(BeautifulSoup(anchor, "html.parser"))


@dataclass
class DownloadQueues:
    pages: set[str] = field(default_factory=set)
    assets: set[str] = field(default_factory=set)


def resolve_url(page: str, href: str) -> str:
    base, frag = urllib.parse.urldefrag(href)
    if "../index.htm" in base:
        return "/Content/" + frag  # _top redirct.
    else:
        return urllib.parse.urljoin(page, base)


def download_and_process_page(page: str, queues: DownloadQueues) -> None:
    """
    Download a page, extract all the data we need, sanitize it and write it to
    the docset folder. Returns the page title.
    """
    tmp_path = download_document(page)
    # Change suffix to .html, if we don't Dash dosen't display titles properly.
    docset_path = (DOCUMENTS_DIR / tmp_path.relative_to(TMP_DIR)).with_suffix(".html")
    docset_path.parent.mkdir(exist_ok=True, parents=True)
    with open(tmp_path) as fd:
        soup = BeautifulSoup(fd, "html.parser")

    # Get links and assets before we sanitize them.
    queues.pages.update(
        resolve_url(page, x["href"]) for x in soup("a", href=is_relative_href)
    )
    queues.assets.update(
        resolve_url(page, x["href"]) for x in soup("link", rel="stylesheet")
    )
    queues.assets.update(resolve_url(page, x["src"]) for x in soup("img"))

    sanitize_html(soup)
    # Support Online Redirection.
    param = page.removeprefix("/Content/")
    comment = f"Online page at https://help.dyalog.com/latest/#{param}"
    soup.html.insert(0, Comment(comment))
    docset_path.write_text(str(soup))
    return soup.title.string


def crawl_pages(queues: DownloadQueues) -> Iterator[tuple[str, str]]:
    """
    Crawl the pages from the provided page queue.
    """
    done_pages = {"/index.htm"}  # Prevents it from ever being downloaded.
    progess = tqdm(total=len(queues.pages), desc="Pages")
    while queues.pages:
        page = queues.pages.pop()
        try:
            title = download_and_process_page(page, queues)
            yield title, page
        except requests.HTTPError as e:
            progess.write(f"Download failed: {e}", file=sys.stderr)
        done_pages.add(page)
        queues.pages -= done_pages
        progess.total = len(queues.pages) + len(done_pages)
        progess.update()
    progess.close()


def create_docset_index(*title_path_iterables: Iterable[tuple[str, str]]):
    """
    Creates a new docset index from given iterables.
    """
    conn = sqlite3.connect(RESOURCES_DIR / "docSet.dsidx")
    conn.execute("DROP TABLE IF EXISTS searchIndex;")
    conn.execute(
        "CREATE TABLE searchIndex(id INTEGER PRIMARY KEY, name TEXT, type TEXT, path TEXT);"
    )
    conn.execute("CREATE UNIQUE INDEX anchor ON searchIndex(name, type, path);")

    for title, path in itertools.chain(*title_path_iterables):
            path = path.removesuffix(".htm") + ".html"
            conn.execute(
                "INSERT OR IGNORE INTO searchIndex(name, type, path) VALUES (?, ?, ?)",
                (title, get_entry_type(path, title), path),
            )

    conn.commit()
    conn.close()


def main() -> None:
    if TMP_DIR.exists():
        print(
            "Note the tmp/ directory already exists. "
            "The docset might contain stale entries. "
            "Remove it if a clean docset is required.",
            file=sys.stderr,
        )

    # Copy the necessary files.
    TMP_DIR.mkdir(exist_ok=True)
    DOCUMENTS_DIR.mkdir(exist_ok=True, parents=True)
    shutil.copyfile("res/Info.plist", DOCSET_DIR / "Contents" / "Info.plist")
    shutil.copyfile("res/icon.png", DOCSET_DIR / "icon.png")

    # Download and process all the pages.
    ride_help = scrape_ride_help()  # Used to generate index entries for APL symbols.
    queues = DownloadQueues(scrape_help_toc() | set(ride_help.values()))
    create_docset_index(crawl_pages(queues), ride_help.items())

    # Download missing assets.
    for asset in tqdm(list(queues.assets), "Assets"):
        tmp_path = download_document(asset)
        docset_path = DOCUMENTS_DIR / tmp_path.relative_to(TMP_DIR)
        docset_path.parent.mkdir(exist_ok=True, parents=True)
        shutil.copyfile(tmp_path, docset_path)


if __name__ == "__main__":
    main()
