# Copyright 2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by the Apache v2.0 license that can be
# found in the LICENSE file.

compile: ../tools/protoc/bin/protoc
	echo Compiling .proto files
	../tools/protoc/bin/protoc --python_out=. *.proto

../tools/protoc/bin/protoc:
	cd ../tools && \
		echo Installing Protocol Buffer Compiler to $(pwd) && \
		./install_protoc.py

