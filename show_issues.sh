#!/bin/bash

git submodule foreach \
  hub issue --format='%sC%>(8)%i%Creset %t% l%n%Cblue%         U%Creset%n' --color=always