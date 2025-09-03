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

import boto3
import requests
import sphobjinv
import jinja2

import bs4

from imgRewrite import fn_imgRewrite

DYNAMIC_LIMIT = 100
NEW_PAGE_SLEEP = 0
BACKOFF_429 = 2


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
    parser.add_argument("--backoff-429", help="Sleep Time for API Calls to Avoid Rate Limit", required=False, type=int, default=BACKOFF_429)
    parser.add_argument("-C", "--confirm", help="Confirm Deletion", action="store_true", default=False)
    parser.add_argument("-3", "--s3Bucket", help="S3 Bucket Name", required=False, default=os.environ.get("S3BUCKET"), type=str)
    parser.add_argument("-A", "--awsprofile", help="AWS Profile Name", required=False, default=os.environ.get("S3PROFILE"), type=str)
    parser.add_argument("-w", "--s3Prefix", help="S3 Prefix", required=False, default=os.environ.get("S3PREFIX", ""), type=str)
    parser.add_argument("-D", "--delete", help="Do Deletion for Dynamic Pages (Default yes)", required=False, default=os.environ.get("DODEL", "yes"), choices=["yes", "no"])

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

    do_img_rewrite = False
    if args.s3Bucket != "none":
        # Setup Profile
        if args.awsprofile == "default":
            # No Profile Data Use the Default Profile
            this_aws_session = boto3.session.Session()
        else:
            # Use a Specific Profile
            this_aws_session = boto3.session.Session(profile_name=args.profile)

        s3_client = this_aws_session.client("s3")

        do_img_rewrite = True

    logger = logging.getLogger("parse_and_upload.py")
    wanted_format = "html"

    all_files = list()
    dynamic_pageId = False
    root_dir = None

    if os.path.isfile(args.objectfile) is False and os.path.isdir(args.objectfile) is False:
        raise FileNotFoundError("No Inventory file or directory found at {}.".format(args.objectfile))
    elif os.path.isfile(args.objectfile):
        all_files = [pathlib.Path(args.objectfile)]

    elif os.path.isdir(args.objectfile):

        dynamic_pageId = True

        for root, _, files in os.walk(args.objectfile):
            for file_name in files:
                if root_dir is None:
                    root_dir = args.objectfile
                this_rel_dir = os.path.relpath(root, args.objectfile)
                this_rel_path = os.path.join(this_rel_dir, file_name)
                this_full_path = os.path.join(root, file_name)
                '''
                logger.info("Filename : {}".format(file_name))
                logger.info("Full Path : {}".format(this_full_path))
                logger.info("Relative Path : {}".format(this_rel_path))
                '''

                if re.search("\\.html$", file_name, re.IGNORECASE):
                    # logger.info("Adding File {this_rel_path} to Upload".format(this_rel_path=this_rel_path))
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

        # Rate Limit Backoff
        time.sleep(args.backoff_429)

        if isinstance(this_filename_obj, (str, pathlib.Path)):
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

                if do_img_rewrite is True:
                    for img in source_html_obj.find_all("img"):
                        if "://" not in img["src"]:
                            # Local Path
                            if root_dir is not None:
                                img_path = os.path.join(root_dir, img["src"])

                                logger.info("img_path: {}".format(img_path))
                            else:
                                # TODO: Handle single files in the future
                                img_path = img["src"]
                                #logger.info("Standard Path: {}".format(img_path))

                            if os.path.isfile(img_path):
                                # This is a File I have locally
                                #logger.info("Rewriting Image {}".format(img["src"]))
                                try:
                                    new_uri = fn_imgRewrite(args.s3Bucket, s3_client, img_path, s3prefix=args.s3Prefix)
                                    img["src"] = new_uri
                                except Exception as e:
                                    logger.error("Rewrite Error when rewriting {img_path}".format(img_path=img_path))
                                    logger.debug("Error: {}".format(e))
                                    continue
                            else:
                                logger.error("Unable to Find Local IMG Path File {}".format(img["src"]))
                                continue
                        else:
                            # This is a uri, ignore it.
                            #logger.info("Ignoring Image URI {}".format(img["src"]))
                            continue


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


        need_put = True

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
                need_put = False

                new_page = "https://coda.io/apis/v1/docs/{doc_id}/pages".format(doc_id=args.docID)

                post_obj = {
                    "name": "{} : {}".format(project_name, this_relpath_name),
                    "subtitle": this_relpath_name,
                    "pageContent": {
                        "type": "canvas",
                        "canvasContent": {
                            "format": "html",
                            "content": rendered_html
                        }
                    }
                }

                if args.staticParentID != "false":
                    logger.info("I have a Static Page Parent ID {}".format(args.staticParentID))
                    post_obj["parentPageId"] = args.staticParentID
                else:
                    logger.info("I have no Static Page Parent ID {}".format(args.staticParentID))
                    raise ValueError("I Should have a Static Parent ID")

                # logger.debug("Creating New Page : {}".format(json.dumps(post_obj, default=str)))
                # logger.debug("Location : {}".format(new_page))

                new_page_response = requests.post(new_page,
                                                  json=post_obj,
                                                  headers={"Authorization": "Bearer " + args.token})

                new_page_response.raise_for_status()

                new_page_data = new_page_response.json()

                # logger.info("New Page Info: {}".format(json.dumps(new_page_data, default=str)))

                pages_uri = urllib.parse.urlparse(
                    "https://coda.io/apis/v1/docs/{doc_id}/pages/{page_id}".format(doc_id=args.docID,
                                                                                   page_id=new_page_data["id"])
                )

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
            if need_put is True:
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
            if need_put is True:
                response_object = {**response_object, **pu_response.json()}
                print(json.dumps(response_object, indent=4))
            else:
                logger.debug("No Put needed on this Page")

    if dynamic_pageId is True:
        logger.info("Future Clean up Unmatched Documents")

        if args.delete == "yes":

            for this_relpath_name, page_cfg in all_pages.items():
                if page_cfg["found_match"] is False:
                    logger.info("Page {} Slated for Deletion".format(this_relpath_name))
                    logger.debug("Page Data {}".format(json.dumps(page_cfg, default=str)))

                    delete_uri = urllib.parse.urlparse(
                        "https://coda.io/apis/v1/docs/{doc_id}/pages/{page_id}".format(doc_id=args.docID,
                                                                                       page_id=page_cfg["og_data"]["id"])
                    )

                    try:
                        del_response = requests.delete(delete_uri.geturl(),
                                                   headers={"Authorization": "Bearer " + args.token,
                                                            "Content-Type": "application/json"
                                                            })
                        del_response.raise_for_status()
                    except Exception as del_error:
                        logger.error("Unable to Delete Page slated for Deletion.")
                        logger.debug(del_error)
        else:
            logger.info("Deletions turned off in Configuration. Ignoring Deletions.")

    sys.exit(0)
