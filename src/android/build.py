"""
Copyright (c) 2011 Michael Elsdoerfer <michael@elsdoerfer.com>

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""

import os, sys
import time
import fnmatch
from os import path
import shutil

from tools import *


__all__ = ('AndroidProject', 'PlatformTarget', 'get_platform',)


class CodeObj(object):
    """Represents a .dex code file.
    """
    def __init__(self, filename):
        self.filename = filename


class ResourceObj(object):
    """Represents a packed resource package."""

    def __init__(self, filename):
        self.filename = filename


class Apk(object):
    """Represents an APK file."""

    def __init__(self, platform, filename):
        self.filename = filename
        self.platform = platform

    def sign(self, *a, **kw):
        return self.platform.sign(self, *a, **kw)

    def align(self, *a, **kw):
        return self.platform.align(self, *a, **kw)


class PlatformTarget(object):
    """Represents a specific platform version provided by the
    Android SDK, and knows how to build Android projects targeting
    this platform.

    The tools and files we need to use as part of the build process
    are partly different in each version.
    """

    def __init__(self, version, sdk_dir, platform_dir, custom_paths={}):
        self.version = version
        self.sdk_dir = sdk_dir
        self.platform_dir = platform_dir

        # The way these path's are officially constructed can be checked
        # in ``com.android.sdklib.PlatformTarget`` and
        # ``com.android.sdklib.SdkConstants``.
        paths = dict(
            aapt =path.join(sdk_dir, 'platform-tools',
                'aapt.exe' if sys.platform=='win32' else 'aapt'),
            aidl = path.join(sdk_dir, 'platform-tools',
                'aidl.exe' if sys.platform=='win32' else 'aidl'),
            dx = path.join(sdk_dir, 'platform-tools',
                'dx.bat' if sys.platform=='win32' else 'dx'),
            apkbuilder = path.join(sdk_dir, 'tools',
                'apkbuilder.bat' if sys.platform=='win32' else 'apkbuilder'),
            zipalign = path.join(sdk_dir, 'tools',
                'zipalign.exe' if sys.platform=='win32' else 'zipalign'),
            jarsigner = 'jarsigner',
            javac = 'javac',
        )
        paths.update(custom_paths)

        self.dx = Dx(paths['dx'])
        self.aapt = Aapt(paths['aapt'])
        self.aidl = Aidl(paths['aidl'])
        self.zipalign = ZipAlign(paths['zipalign'])
        self.apkbuilder = ApkBuilder(paths['apkbuilder'])
        self.javac = JavaC(paths['javac'])
        self.jarsigner = JarSigner(paths['jarsigner'])

        self.framework_library = path.join(platform_dir, 'android.jar')
        self.framework_aidl = path.join(platform_dir, 'framework.aidl')

    def __repr__(self):
        return 'Platform %s <%s>' % (self.version, self.platform_dir)

    def generate_r(self, manifest, resource_dir, output_dir):
        """Generate the R.java file in ``output_dir``, based
        on ``resource_dir``.

        Final call will look something like this::

            $ aapt package -m -J gen/ -M AndroidManifest.xml -S res/
                -I android.jar
        """
        mkdir(output_dir)
        self.aapt(
            command='package',
            make_dirs=True,
            manifest=manifest,
            resource_dir=resource_dir,
            r_output=output_dir,
            include=[self.framework_library],)

    def compile_aidl(self, source_dir, output_dir):
        """Compile .aidl definitions found in ``source_dir`` into
        Java files, and put them into ``output_dir``.

        Final call will look something like this::

            $ aidl -pframework.aidl -Isrc/ -ogen/ Foo.aidl
        """
        for filename in recursive_glob(source_dir, '*.aidl'):
            self.aidl(
                filename,
                preprocessed=self.framework_aidl,
                search_path=source_dir,
                output_folder=output_dir,
            )

    def compile_java(self, source_dirs, output_dir, debug=False,
                     target='1.5'):
        """Compile all *.java files in ``source_dirs`` (a list of
        directories) and store the class files in ``output_dir``.
        """
        # Collect all files to be compiled
        files = []
        for directory in source_dirs:
            files += recursive_glob(directory, '*.java')
        # TODO: check if files are up-to-date?
        # TODO: Include libs/*.jar as -classpath
        mkdir(output_dir, True)
        self.javac(
            files,
            target=target,
            debug=debug,
            destdir=output_dir,
            bootclasspath=self.framework_library)

    def dex(self, source_dir, output):
        """Dexing is the process of converting Java bytecode to Dalvik
        bytecode.

        Will process all class files in ``source_dir`` and store the
        result in a single file ``output``.

        Final call will look somethin like this::

            $ dx --dex --output=bin/classes.dex bin/classes libs/*.jar
        """
        # TODO: Include libs/*.jar
        output = path.abspath(output)
        self.dx([source_dir], output=output)
        return CodeObj(output)

    def compile(self, dex_output, manifest, source_dir, resource_dir,
                source_gen_dir, class_gen_dir, **kwargs):
        """Shortcut for the whole process until dexing into a code
        object that we can pack into an APK.
        """
        self.generate_r(manifest, resource_dir, source_gen_dir)
        self.compile_aidl(source_dir, source_gen_dir)
        self.compile_java([source_dir, source_gen_dir],
                          class_gen_dir, **kwargs)
        return self.dex(class_gen_dir, dex_output)

    def pack_resources(self, manifest, resource_dir, asset_dir=None,
                       output=None, configurations=None):
        """Package all the resource files.

        ``configurations`` may be a list of configuration values to be
        included. For example: "de" to make a German-only build, or
        "port,land,en_US". By default, all configurations are built.

            $ aapt package -f -M AndroidManifest.xml -S res/
                -A assets/ -I android.jar -F out/BASE-CONFIG.ap_
        """
        output = path.abspath(output)
        kwargs = dict(
            command='package',
            manifest=manifest,
            resource_dir=resource_dir,
            include=[self.framework_library],
            apk_output=output,
            configurations=configurations,
            # There is no error code without overwrite, so
            # let's not even give the user the choice, it
            # would only cause confusion.
            overwrite=True)
        if asset_dir:
            kwargs['asset_dir'] = asset_dir
        self.aapt(**kwargs)
        return ResourceObj(output)

    def build_apk(self, output, code=None, resources=None):
        """Build an APK file, using the given resource package.
        """
        # TODO: Add libs/ (rj, nf options).
        output = path.abspath(output)
        kwargs = dict(outputfile=output)
        if code:
            kwargs['dex'] = code.filename \
                  if isinstance(code, CodeObj) else code
        if resources:
            kwargs['zips'] = [resources.filename \
                  if isinstance(resources, ResourceObj) else resources]
        self.apkbuilder(**kwargs)
        return Apk(self, output)

    def sign(self, apk, keystore, alias, password):
        """Sign an APK file.
        """
        self.jarsigner(
            apk.filename if isinstance(apk, Apk) else apk,
            keystore=keystore, alias=alias, password=password)

    def align(self, apk, output=None):
        """Align an APK file.

        If ``outfile`` is not given, the APK is align in place.
        """
        infile = apk.filename if isinstance(apk, Apk) else apk
        if not output:
            # Or should tempfile be used? Might be on another
            # filesystem though.
            outfile = "%s.align.%s" % (infile, time.time())
        self.zipalign(infile, outfile, align=4, force=True)

        if not output:
            # In-place align was requested, return the original file
            os.rename(outfile, infile)
            return apk
        else:
            # Return a new APK.
            return Apk(self, outfile)


def get_platform(sdk_path, target=None):
    """Return path and filename information for the given SDK target.

    If no target is given, the most recent target is chosen.
    """
    platforms = filter(lambda p: path.isdir(p),
                       map(lambda e: path.join(sdk_path, 'platforms', e),
                           os.listdir(path.join(sdk_path, 'platforms'))))
    # Gives us a dict like {'10': '/sdk/platforms/android-10'}
    platforms = dict([(p.rsplit('-', 1)[1], p) for p in platforms])

    if not target:
        # Use the latest target - Python string sorting is smart
        # enough here to do the right thing.
        target = sorted(platforms.keys())[-1]

    try:
        target_root = platforms[target]
    except KeyError:
        raise ValueError('target "%s" not found in "%s"' % (
            target, sdk_path))

    return PlatformTarget(target, sdk_path, target_root)


def recursive_glob(treeroot, pattern):
    """From: http://stackoverflow.com/questions/2186525/2186639#2186639"""
    results = []
    for base, dirs, files in os.walk(treeroot):
        goodfiles = fnmatch.filter(files, pattern)
        results.extend(os.path.join(base, f) for f in goodfiles)
    return results


def mkdir(directory, recursive=False):
    if not path.exists(directory):
        if recursive:
            os.makedirs(directory)
        else:
            os.mkdir(directory)


class AndroidProject(object):
    """Represents an Android project to be built.

    This provides a more high-level approach than working with
    ``PlatformTarget`` directly, by making some default assumptions
    as to directory layout and file locations.
    """

    def __init__(self, manifest, name=None, platform=None, sdk_dir=None,
                 target=None):
        if not platform:
            if not sdk_dir:
                raise ValueError('You need to provide the SDK path, '
                                 'or a PlatformTarget instancen.')
            platform = get_platform(sdk_dir, target)

        self.platform = platform

        # Project-specific paths
        self.manifest = path.abspath(manifest)
        project_dir = path.dirname(self.manifest)
        self.resource_dir = path.join(project_dir, 'res')
        self.gen_dir = path.join(project_dir, 'gen')
        self.source_dir = path.join(project_dir, 'src')
        self.out_dir = path.join(project_dir, 'bin')
        self.asset_dir = path.join(project_dir, 'asset')

        if not name:
            # if no name is given, inspect the manifest
            from xml.etree import ElementTree
            tree = ElementTree.parse(self.manifest)
            name = tree.getroot().attrib['package']
        self.name = name

    def compile(self):
        """Force a recompile of the project.
        """
        self.code = self.platform.compile(
            dex_output=path.join(self.out_dir, 'classes.dex'),
            manifest=self.manifest,
            source_dir=self.source_dir,
            resource_dir=self.resource_dir,
            source_gen_dir=self.gen_dir,
            class_gen_dir=path.join(self.out_dir, 'classes')
        )

    def build(self, output=None, config=None):
        """Shortcut to build everything into a final APK in one step.
        """
        # Make sure the code is compiled
        if not hasattr(self, 'code'):
            self.compile()

        # Package the resources
        if not config:
            resource_filename = path.join(
                self.out_dir, '%s.ap_' % (self.name))
        else:
            resource_filename = path.join(
                self.out_dir, '%s.%s.ap_' % (self.name, config))
        kwargs = dict(
            manifest=self.manifest,
            resource_dir=self.resource_dir,
            configurations=config,
            output=resource_filename,
        )
        if path.exists(self.asset_dir):
            kwargs.update({'asset_dir': self.asset_dir})
        resources = self.platform.pack_resources(**kwargs)

        # Put everything into an APK.
        apk = self.platform.build_apk(
            path.join(self.out_dir, '%s.apk' % self.name),
            code=self.code, resources=resources)
        return apk

    def clean(self):
        """Deletes both ``self.out_dir`` and ``self.gen_dir``.
        """
        if path.exists(self.out_dir):
            shutil.rmtree(self.out_dir)
        if path.exists(self.out_dir):
            shutil.rmtree(self.gen_dir)
