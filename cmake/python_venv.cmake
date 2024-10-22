# (C) Copyright 2018- ECMWF.
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.

##############################################################################
#.rst:
#
# find_python_venv
# ================
#
# Find Python 3 inside a virtual environment. ::
#
#   find_python_venv(VENV_PATH)
#
# It finds the Python3 Interpreter from a virtual environment at
# the given location (`VENV_PATH`)
#
# Options
# -------
#
# :VENV_PATH: The path to the virtual environment
# :PYTHON_VERSION: Permissible Python versions for find_package
#
# Output variables
# ----------------
# :Python3_FOUND:       Exported into parent scope from FindPython3
# :Python3_EXECUTABLE:  Exported into parent scope from FindPython3
# :Python3_VENV_BIN:    The path to the virtual environment's `bin` directory
# :ENV{VIRTUAL_ENV}:    Environment variable with the virtual environment directory,
#                       emulating the activate script
#
##############################################################################

function( find_python_venv VENV_PATH PYTHON_VERSION )

    # Update the environment with VIRTUAL_ENV variable (mimic the activate script)
    set( ENV{VIRTUAL_ENV} ${VENV_PATH} )

    # Change the context of the search to only find the venv
    set( Python3_FIND_VIRTUALENV ONLY )

    # Unset Python3_EXECUTABLE because it is also an input variable
    #  (see documentation, Artifacts Specification section)
    unset( Python3_EXECUTABLE )
    # To allow cmake to discover the newly created venv if Python3_ROOT_DIR
    # was passed as an argument at build-time
    set( Python3_ROOT_DIR "${VENV_PATH}" )

    # Launch a new search
    find_package( Python3 ${PYTHON_VERSION} COMPONENTS Interpreter REQUIRED )

    # Find the binary directory of the virtual environment
    execute_process(
        COMMAND ${Python3_EXECUTABLE} -c "import sys; import os.path; print(os.path.dirname(sys.executable), end='')"
        OUTPUT_VARIABLE Python3_VENV_BIN
    )

    # Forward variables to parent scope
    foreach ( _VAR_NAME Python3_FOUND Python3_EXECUTABLE Python3_VENV_BIN )
        set( ${_VAR_NAME} ${${_VAR_NAME}} PARENT_SCOPE )
    endforeach()

endfunction()

##############################################################################
#.rst:
#
# create_python_venv
# ==================
#
# Find Python 3 and create a virtual environment. ::
#
#   create_python_venv(VENV_PATH)
#
# Installation procedure
# ----------------------
#
# It creates a virtual environment at the given location (`VENV_PATH`)
#
# Options
# -------
#
# :VENV_PATH: The path to use for the virtual environment
# :PYTHON_VERSION: Permissible Python versions for find_package
#
##############################################################################

function( create_python_venv VENV_PATH PYTHON_VERSION )

    # Discover only system install Python 3
    set( Python3_FIND_VIRTUALENV STANDARD )
    find_package( Python3 ${PYTHON_VERSION} COMPONENTS Interpreter REQUIRED )

    # Ensure the activate script is writable in case the venv exists already
    if( EXISTS "${VENV_PATH}/bin/activate" )
        file( CHMOD "${VENV_PATH}/bin/activate" FILE_PERMISSIONS OWNER_READ OWNER_WRITE )
    endif()

    # Create a loki virtualenv
    ecbuild_info( "Create Python virtual environment ${VENV_PATH}" )
    execute_process( COMMAND ${Python3_EXECUTABLE} -m venv --copies "${VENV_PATH}" )

    # Make the virtualenv portable by automatically deducing the VIRTUAL_ENV path from
    # the 'activate' script's location in the filesystem
    file( READ "${VENV_PATH}/bin/activate" FILE_CONTENT )
    string(
        REGEX REPLACE
            "\nVIRTUAL_ENV=\".*\"\nexport VIRTUAL_ENV"
            "\nVIRTUAL_ENV=\"$(readlink -f \"$(dirname \"$(dirname \"\${BASH_SOURCE[0]}\")\")\")\"\nexport VIRTUAL_ENV"
        FILE_CONTENT
        "${FILE_CONTENT}"
    )
    file( WRITE "${VENV_PATH}/bin/activate" "${FILE_CONTENT}" )

endfunction()

##############################################################################
#.rst:
#
# setup_python_venv
# =================
#
# Find Python 3, create a virtual environment and make it available. ::
#
#   setup_python_venv(VENV_PATH)
#
# It combines calls to `create_python_venv` and `find_python_venv`
#
# Options
# -------
#
# :VENV_PATH: The path to use for the virtual environment
# :PYTHON_VERSION: Permissible Python versions for find_package
#
# Output variables
# ----------------
# :Python3_FOUND:       Exported into parent scope from FindPython3
# :Python3_EXECUTABLE:  Exported into parent scope from FindPython3
# :Python3_VENV_BIN:    The path to the virtual environment's `bin` directory
# :ENV{VIRTUAL_ENV}:    Environment variable with the virtual environment directory,
#                       emulating the activate script
#
##############################################################################

macro( setup_python_venv VENV_PATH PYTHON_VERSION )

    # Create the virtual environment
    create_python_venv( ${VENV_PATH} ${PYTHON_VERSION} )

    # Discover Python in the virtual environment and set-up variables
    find_python_venv( ${VENV_PATH} ${PYTHON_VERSION} )

endmacro()

##############################################################################
#.rst:
#
# update_python_shebang
# =====================
#
# Update the shebang in the given executable scripts to link them to a
# Python executable that is located in the same directory. ::
#
#   update_python_shebang( executable1 [executable2] [...] )
#
##############################################################################

function( update_python_shebang )

    foreach( _exe IN LISTS ARGV )

        # Replace the shebang in the executable script by the following to use the
        # Python binary that resides in the same directory as the script
        # (see https://stackoverflow.com/a/57567228).
        # That allows to move the script elsewhere along with the rest of the virtual
        # environment without breaking the link to the venv-interpreter
        #
        # #!/bin/sh
        # "true" '''\'
        # exec "$(dirname "$(readlink -f "$0")")"/python "$0" "$@"
        # '''

        ecbuild_debug( "Update shebang for ${_exe}" )
        file( READ "${_exe}" FILE_CONTENT )
        string(
            REGEX REPLACE
                "#!/.*\n#"
                "#!/bin/sh\n\"true\" '''\\\\'\nexec \"$(dirname \"$(readlink -f \"\$0\")\")/python\" \"\$0\" \"\$@\"\n'''\n#"
            FILE_CONTENT
            "${FILE_CONTENT}"
        )
        file( WRITE "${_exe}" "${FILE_CONTENT}" )

    endforeach()

endfunction()
