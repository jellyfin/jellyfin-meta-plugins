#!/bin/bash

git submodule foreach \
  hub pr list --format='%pC%>(8)%i%Creset %t% l%n%Cblue%         U%Creset%n' --color=always
