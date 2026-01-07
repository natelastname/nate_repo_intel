# -*- coding: utf-8 -*-
"""
Created on 2026-01-07T17:02:22-05:00

@author: nate
"""
import argh
from loguru import logger

import nate_repo_intel


def main():
    logger.info(__name__)

def cli():
    parser = argh.ArghParser()
    parser.add_commands([
            main
    ])
    parser.dispatch()

    # Only one entrypoint
    #argh.dispatch_command(main)