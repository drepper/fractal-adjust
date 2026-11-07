# Name and version come from their single sources (pyproject.toml, fractal_adjust/__init__.py)
# through tools/gen-spec.py, which also renders the spec file from fractal-adjust.spec.in.
GEN_SPEC := python3 tools/gen-spec.py
NAME := $(shell $(GEN_SPEC) --get NAME)
VERSION := $(shell $(GEN_SPEC) --get VERSION)
MODULE := $(shell $(GEN_SPEC) --get MODULE)
TARBALL := $(NAME)-$(VERSION).tar.gz
DIST_FILES := $(sort Makefile pyproject.toml LICENSE $(filter-out CLAUDE.md,$(wildcard *.md)) \
	$(NAME).spec $(NAME).spec.in \
	$(wildcard tools/*.py $(MODULE)/*.py $(MODULE)/data/* data/* tests/*.py))
RPMBUILD := rpmbuild --define "_sourcedir $(CURDIR)" --define "_srcrpmdir $(CURDIR)" \
	--define "_rpmdir $(CURDIR)/rpms"

.PHONY: all spec dist srpm rpm check clean

all: spec

spec: $(NAME).spec

$(NAME).spec: $(NAME).spec.in pyproject.toml $(MODULE)/__init__.py $(MODULE)/gui.py tools/gen-spec.py
	$(GEN_SPEC) $< > $@.tmp && mv $@.tmp $@

dist: $(TARBALL)

$(TARBALL): $(DIST_FILES)
	tar czf $@ --transform 's,^,$(NAME)-$(VERSION)/,' $(DIST_FILES)

srpm: $(TARBALL) $(NAME).spec
	$(RPMBUILD) -bs $(NAME).spec

rpm: $(TARBALL) $(NAME).spec
	$(RPMBUILD) -bb $(NAME).spec

check:
	python3 -m pytest -q
	python3 -m mypy $(MODULE) tests tools

clean:
	rm -rf $(NAME).spec $(NAME).spec.tmp $(NAME)-*.tar.gz $(NAME)-*.src.rpm rpms build *.egg-info
