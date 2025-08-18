#!/usr/bin/env python3

"""
Parse Objects file, Use that to create an html index. That html index should get uploaded to COAD.io
"""

import argparse
import logging
import os
import urllib.parse
import pathlib
import json
import time
import sys
import re

import requests
import sphobjinv
import jinja2

import bs4

DYNAMIC_LIMIT = 100
NEW_PAGE_SLEEP = 3


def get_argparse():
    """
    Let's grab my runtime options
    """

    parser = argparse.ArgumentParser()

    parser.add_argument("-b", "--uribase", help="Hosted Sphinx Domain", required=False,
                        default=os.environ.get("SPHINX_BASE_URI"))
    parser.add_argument("-f", "--objectfile", help="Intersphinx File", required=False,
                        default=os.environ.get("OBJECTS_FILE", "build/html/objects.inv"))
    parser.add_argument("-c", "--linkclass", help="For HTML Files, what class are internal links saved as.",
                        required=False,
                        default=os.environ.get("LINK_CLASS", "reference internal"))
    parser.add_argument("-i", "--docID", help="CodaIO Document (Category) ID", required=False,
                        default=os.environ.get("DOCID"))
    parser.add_argument("-p", "--pageID", help="PageID for Coda", required=False,
                        default=os.environ.get("PAGEID"))
    parser.add_argument("-S", "--staticParentID", help="For Dynamic Pages, Only consider Children of this Page", required=False,
                        default=os.environ.get("STATICPID", "false"))
    parser.add_argument("--token", help="Coda.io API Token", required=False,
                        default=os.environ.get("CODA_TOKEN"))
    parser.add_argument("-v", "--verbose", action="append_const", help="Verbosity Controls",
                        const=1, default=[])
    parser.add_argument("-t", "--template", help="HTML Template File", required=False,
                        default=os.environ.get("TEMPLATE", "src/template.html.jinja"))
    parser.add_argument("--new-page-sleep", help="Sleep Time for New Pages", required=False, type=int, default=NEW_PAGE_SLEEP)
    parser.add_argument("-C", "--confirm", help="Confirm Deletion", action="store_true", default=False)

    return parser


if __name__ == "__main__":

    parser = get_argparse()

    args = parser.parse_args()

    VERBOSE = len(args.verbose)

    if VERBOSE == 0:
        logging.basicConfig(level=logging.ERROR)
    elif VERBOSE == 1:
        logging.basicConfig(level=logging.WARNING)
    elif VERBOSE == 2:
        logging.basicConfig(level=logging.INFO)
    elif VERBOSE > 2:
        logging.basicConfig(level=logging.DEBUG)

    logger = logging.getLogger("parse_and_upload.py")
    wanted_format = "html"

    all_files = list()
    dynamic_pageId = False

    if os.path.isfile(args.objectfile) is False and os.path.isdir(args.objectfile) is False:
        raise FileNotFoundError("No Inventory file or directory found at {}.".format(args.objectfile))
    elif os.path.isfile(args.objectfile):
        all_files = [pathlib.Path(args.objectfile)]

    elif os.path.isdir(args.objectfile):

        dynamic_pageId = True

        for root, _, files in os.walk(args.objectfile):
            for file_name in files:
                this_rel_dir = os.path.relpath(root, args.objectfile)
                this_rel_path = os.path.join(this_rel_dir, file_name)
                this_full_path = os.path.join(root, file_name)
                logger.info("Filename : {}".format(file_name))
                logger.info("Full Path : {}".format(this_full_path))
                logger.info("Relative Path : {}".format(this_rel_path))

                if re.search("\\.html$", file_name, re.IGNORECASE):
                    logger.info("Adding File {this_rel_path} to Upload".format(this_rel_path=this_rel_path))
                    all_files.append({"name_relpath": this_rel_path, "name_fullpath": this_full_path})
                else:
                    logger.info("Ignoring File {this_full_path} not in".format(this_full_path=this_full_path))

        # Collect all Pages in DocID
        all_docs_uri = urllib.parse.urlparse("https://coda.io/apis/v1/docs/{doc_id}/pages".format(doc_id=args.docID))

        get_more = True
        extra_params = dict()
        all_pages = dict()

        while get_more:
            all_pages_response = requests.get(all_docs_uri.geturl(),
                                              params={**extra_params},
                                              headers={"Authorization": "Bearer " + args.token})

            all_pages_response.raise_for_status()

            results = all_pages_response.json()

            # Handle Multiple
            if "nextPageToken" in results.keys():
                get_more = True
                logger.info("More Pages to Get : {}".format(results["nextPageToken"]))
                extra_params["pageToken"] = results["nextPageToken"]
            else:
                get_more = False

            for this_page_details in results["items"]:

                if args.staticParentID == "false" or this_page_details.get("parent", {"id": None})["id"] == args.staticParentID:

                    logger.info("Found Page {} Subtitled: {}".format(this_page_details["name"], this_page_details["subtitle"]))
                    logger.debug(this_page_details)
                    all_pages[this_page_details["subtitle"]] = {"og_data": this_page_details,
                                                                "found_match": False,
                                                                "alt_parent": None}
                else:
                    #logger.debug("Found Page With Incorrect Parent {} Subtitled: {}".format(this_page_details["name"], this_page_details["subtitle"]))
                    pass

    for this_filename_obj in all_files:

        if isinstance(this_filename_obj, str):
            this_filename = this_filename_obj
            this_relpath_name = this_filename_obj
        elif isinstance(this_filename_obj, dict):
            this_filename = this_filename_obj["name_fullpath"]
            this_relpath_name = this_filename_obj["name_relpath"]

        intersphinx_file = pathlib.Path(this_filename)
        response_object = {"update_time": time.ctime()}
        project_name = "Unspecified"

        if intersphinx_file.suffix == ".inv":

            intersphinx_inventory = sphobjinv.Inventory(args.objectfile)
            project_name = intersphinx_inventory.project

            with open(args.template, "r") as template_fobj:
                template_string = template_fobj.read()

                html_template = jinja2.Environment(loader=jinja2.BaseLoader,
                                                   autoescape=jinja2.select_autoescape(
                                                       enabled_extensions=('html', 'xml'),
                                                       default_for_string=False,
                                                   )).from_string(template_string)

                rendered_html = html_template.render({"inventory": intersphinx_inventory,
                                                      "baseuri": args.uribase})

        elif intersphinx_file.suffix == ".html":

            logger.info("Reading and Cleaning HTML File {}".format(this_filename))

            with open(intersphinx_file, "r") as source_fobj:

                source_html_obj = bs4.BeautifulSoup(source_fobj, features="html.parser")
                project_name = source_html_obj.title.string

                # Strip some stuff
                for item in source_html_obj.contents[:10]:
                    if isinstance(item, bs4.Doctype):
                        item.extract()

                for data in source_html_obj(["style", "script", "svg", "link",
                                             "meta", "input", "label", "header",
                                             "aside", "button", "symbol"]):
                    data.decompose()

                for alink in source_html_obj.find_all("a", attrs={'class': args.linkclass}):
                    if alink["href"].startswith("#"):
                        alink.decompose()
                    else:
                        new_url = urllib.parse.urljoin(args.uribase, alink["href"])

                        alink["href"] = new_url

                for selflink in source_html_obj.find_all("a"):
                    if selflink["href"].startswith("#"):
                        selflink.decompose()

                for span_strip in source_html_obj.find_all("span"):
                    span_strip.unwrap()

                # Handle Admonitions
                for admonition_div in source_html_obj.find_all('div', {'class': "admonition"}):
                    admonition_div.wrap(source_html_obj.new_tag("aside"))

                spacey_rendered_html = str(source_html_obj)  # .replace("\n", "")
                rendered_html = re.sub(r"\n+", "\n", spacey_rendered_html)

                # print(rendered_html)

        ## Coda Stuff

        if dynamic_pageId is False:
            # There's a Single, Specified Page

            pages_uri = urllib.parse.urlparse(
                "https://coda.io/apis/v1/docs/{doc_id}/pages/{page_id}".format(doc_id=args.docID,
                                                                               page_id=args.pageID))

            update_payload = {
                "name": project_name,
                "subtitle": "Generated Time: {ctime}".format(ctime=response_object["update_time"]),
                "contentUpdate": {
                    "insertionMode": "replace",
                    "canvasContent": {
                        "format": "html",
                        "content": rendered_html
                    }
                }
            }

        else:
            # Dynamic Pages

            if this_relpath_name in all_pages.keys():

                pages_uri = urllib.parse.urlparse("https://coda.io/apis/v1/docs/{doc_id}/pages/{page_id}".format(doc_id=args.docID,
                                                                                           page_id=
                                                                                           all_pages[this_relpath_name][
                                                                                               "og_data"]["id"]))

                all_pages[this_relpath_name]["found_match"] = True


            else:
                # Dynamic Page Generation
                # Create a New Page

                new_page = "https://coda.io/apis/v1/docs/{doc_id}/pages".format(doc_id=args.docID)

                post_obj = {
                    "name": "{} : {}".format(project_name, this_relpath_name),
                    "subtitle": this_relpath_name,
                }

                if args.staticParentID != "false":
                    logger.info("I have a Static Page Parent ID {}".format(args.staticParentID))
                    post_obj["parentPageId"] = args.staticParentID
                else:
                    logger.info("I have no Static Page Parent ID {}".format(args.staticParentID))
                    raise ValueError("I Should have a Static Parent ID")

                logger.debug("Creating New Page : {}".format(json.dumps(post_obj, default=str)))
                logger.debug("Location : {}".format(new_page))

                new_page_response = requests.post(new_page,
                                                  json=post_obj,
                                                  headers={"Authorization": "Bearer " + args.token})

                new_page_response.raise_for_status()

                new_page_data = new_page_response.json()

                logger.info("New Page Info: {}".format(json.dumps(new_page_data, default=str)))

                pages_uri = urllib.parse.urlparse(
                    "https://coda.io/apis/v1/docs/{doc_id}/pages/{page_id}".format(doc_id=args.docID,
                                                                                   page_id=new_page_data["id"])
                )

                logger.debug("New Page Sleep for {}".format(args.new_page_sleep))
                time.sleep(args.new_page_sleep)

            update_payload = {
                "name": "{} - {}".format(project_name, this_relpath_name),
                "subtitle": this_relpath_name,
                "contentUpdate": {
                    "insertionMode": "replace",
                    "canvasContent": {
                        "format": "html",
                        "content": rendered_html
                    }
                }
            }

        try:
            page_response = requests.get(pages_uri.geturl(),
                                         headers={"Authorization": "Bearer " + args.token})

            page_response.raise_for_status()
        except Exception as page_error:
            logger.error("Unable to find Specified Pages. Possibly a Permissions or Existence Error.")
            logger.debug(page_error)
            sys.exit(1)
        else:

            ''' Legacy Replaced By Logic Above
            update_payload = {
                "name": project_name,
                "subtitle": "Generated Time: {ctime}".format(ctime=response_object["update_time"]),
                "contentUpdate": {
                    "insertionMode": "replace",
                    "canvasContent": {
                        "format": "html",
                        "content": rendered_html
                    }
                }
            }
            '''

            try:
                pu_response = requests.put(pages_uri.geturl(),
                                           headers={"Authorization": "Bearer " + args.token,
                                                    "Content-Type": "application/json"
                                                    },
                                           json=update_payload
                                           )
                pu_response.raise_for_status()
            except Exception as pu_error:
                logger.error("Unable to Update the Page.")
                logger.debug(pu_error)
                sys.exit(1)
            else:
                # I've updated the page
                response_object = {**response_object, **pu_response.json()}
                print(json.dumps(response_object, indent=4))

    if dynamic_pageId is True:
        logger.info("Future Clean up Unmatched Documents")
        pass

    sys.exit(0)
