# (C) Copyright 2018- ECMWF.
# This software is licensed under the terms of the Apache Licence Version 2.0
# which can be obtained at http://www.apache.org/licenses/LICENSE-2.0.
# In applying this licence, ECMWF does not waive the privileges and immunities
# granted to it by virtue of its status as an intergovernmental organisation
# nor does it submit to any jurisdiction.

import pytest

from loki import Dimension
from loki.batch import Scheduler, SchedulerConfig
from loki.expression import InlineCall, simplify
from loki.frontend import available_frontends, OMNI, FP, OFP
from loki.ir import (
    FindNodes, CallStatement, Assignment, Allocation, Deallocation,
    Loop, Pragma, get_pragma_parameters, FindVariables, FindInlineCalls
)

from loki.transformations.pool_allocator import TemporariesPoolAllocatorTransformation


@pytest.fixture(scope='module', name='block_dim')
def fixture_block_dim():
    return Dimension(name='block_dim', size='nb', index='b')

@pytest.fixture(scope='module', name='horizontal')
def fixture_horizontal():
    return Dimension(name='horizontal', size='nlon', index='jl', bounds=('start', 'end'), aliases=('klon', 'columns'))

@pytest.fixture(scope='module', name='block_dim_alt')
def fixture_block_dim_alt():
    return Dimension(name='block_dim_alt', size='geom%blk_dim%nb', index='b')

def check_c_sizeof_import(routine):
    assert any(import_.module.lower() == 'iso_c_binding' for import_ in routine.imports)
    assert 'c_sizeof' in routine.imported_symbols

def remove_redundant_substrings(text, kind_real=None):
    text = text.replace(f'/max(c_sizeof(real(1,kind={kind_real})),8)', '')
    text = text.replace(f'*max(c_sizeof(real(1,kind={kind_real})),8)', '')
    text = text.replace(f'max(c_sizeof(real(1,kind={kind_real})),8)*', '')
    text = text.replace(f'max(c_sizeof(real(1,kind={kind_real})),8)', '')
    text = text.replace(f'/c_sizeof(real(1,kind={kind_real}))', '')
    text = text.replace(f'*c_sizeof(real(1,kind={kind_real}))', '')
    text = text.replace(f'c_sizeof(real(1,kind={kind_real}))*', '')
    text = text.replace(f'c_sizeof(real(1,kind={kind_real}))', '')
    text = text.replace('/max(c_sizeof(real(1,kind=jprb)),8)', '')
    text = text.replace('*max(c_sizeof(real(1,kind=jprb)),8)', '')
    text = text.replace('max(c_sizeof(real(1,kind=jprb)),8)*', '')
    text = text.replace('max(c_sizeof(real(1,kind=jprb)),8)', '')
    text = text.replace('/c_sizeof(real(1,kind=jprb))', '')
    text = text.replace('*c_sizeof(real(1,kind=jprb))', '')
    text = text.replace('c_sizeof(real(1,kind=jprb))*', '')
    text = text.replace('c_sizeof(real(1,kind=jprb))', '')
    return text

def check_stack_created_in_driver(
        driver, stack_size, first_kernel_call, num_block_loops,
        generate_driver_stack=True, kind_real='jprb', check_bounds=True, simplify_stmt=True,
        cray_ptr_loc_rhs=False
):
    # Are stack size, storage and stack derived type declared?
    assert 'istsz' in driver.variables
    assert 'zstack(:,:)' in driver.variables
    assert 'ylstack_l' in driver.variables

    # Is there an allocation and deallocation for the stack storage?
    allocations = FindNodes(Allocation).visit(driver.body)
    assert len(allocations) == 1 and 'zstack(istsz,nb)' in allocations[0].variables
    deallocations = FindNodes(Deallocation).visit(driver.body)
    assert len(deallocations) == 1 and 'zstack' in deallocations[0].variables

    # Check the stack size
    assignments = FindNodes(Assignment).visit(driver.body)
    for assignment in assignments:
        if assignment.lhs == 'istsz':
            if simplify_stmt:
                assert str(simplify(assignment.rhs)).lower().replace(' ', '') \
                           == str(stack_size).lower().replace(' ', '')
            else:
                assert str(assignment.rhs).lower().replace(' ', '') == str(stack_size).lower().replace(' ', '')
            break

    # Check for stack assignment inside loop
    loops = FindNodes(Loop).visit(driver.body)
    assert len(loops) == num_block_loops
    assignments = FindNodes(Assignment).visit(loops[0].body)
    assert assignments[0].lhs == 'ylstack_l'
    if cray_ptr_loc_rhs:
        assert assignments[0].rhs == '1'
    else:
        assert isinstance(assignments[0].rhs, InlineCall) and assignments[0].rhs.function == 'loc'
        assert 'zstack(1, b)' in assignments[0].rhs.parameters
    if check_bounds:
        if generate_driver_stack:
            if cray_ptr_loc_rhs:
                assert assignments[1].lhs == 'ylstack_u' and (
                        assignments[1].rhs == 'ylstack_l + istsz')
            else:
                assert assignments[1].lhs == 'ylstack_u' and (
                        assignments[1].rhs == f'ylstack_l + istsz * c_sizeof(real(1, kind={kind_real}))')
        else:
            if cray_ptr_loc_rhs:
                assert assignments[1].lhs == 'ylstack_u' and (
                        assignments[1].rhs == 'ylstack_l + istsz')
            else:
                assert assignments[1].lhs == 'ylstack_u' and (
                        assignments[1].rhs == f'ylstack_l + c_sizeof(real(1, kind={kind_real}))*istsz')

            if cray_ptr_loc_rhs:
                expected_rhs = 'ylstack_l + istsz'
            else:
                expected_rhs = f'ylstack_l + c_sizeof(real(1, kind={kind_real}))*istsz'
            assert assignments[1].lhs == 'ylstack_u' and assignments[1].rhs == expected_rhs

    # Check that stack assignment happens before kernel call
    assert all(loops[0].body.index(a) < loops[0].body.index(first_kernel_call) for a in assignments)


@pytest.mark.parametrize('generate_driver_stack', [False, True])
@pytest.mark.parametrize('frontend', available_frontends())
@pytest.mark.parametrize('check_bounds', [False, True])
@pytest.mark.parametrize('nclv_param', [False, True])
@pytest.mark.parametrize('cray_ptr_loc_rhs', [False, True])
def test_pool_allocator_temporaries(tmp_path, frontend, generate_driver_stack, block_dim, check_bounds,
                                    nclv_param, cray_ptr_loc_rhs, horizontal):
    fcode_iso_c_binding = "use, intrinsic :: iso_c_binding, only: c_sizeof"
    fcode_nclv_param = 'integer, parameter :: nclv = 2'
    if frontend == OMNI:
        if cray_ptr_loc_rhs:
            fcode_stack_decl = f"""
            integer :: istsz
            REAL(KIND=JPRB), ALLOCATABLE :: ZSTACK(:, :)
            integer(kind=8) :: ylstack_l
            integer(kind=8) :: ylstack_u

            {'istsz = 3*nlon+nlon*nz' if nclv_param else 'istsz = 3*nlon+nlon*nz+2'}
            ALLOCATE(ZSTACK(ISTSZ, nb))
            """
        else:
            fcode_stack_decl = f"""
            integer :: istsz
            REAL(KIND=JPRB), ALLOCATABLE :: ZSTACK(:, :)
            integer(kind=8) :: ylstack_l
            integer(kind=8) :: ylstack_u

            {'istsz = 3*nlon+nlon*nz' if nclv_param else 'istsz = 3*nlon+nlon*nz+2*max(c_sizeof(real(1,kind=jprb)), 8)/c_sizeof(real(1,kind=jprb))'}
            ALLOCATE(ZSTACK(ISTSZ, nb))
            """
    else:
        if cray_ptr_loc_rhs:
            fcode_stack_decl = f"""
            integer :: istsz
            REAL(KIND=JPRB), ALLOCATABLE :: ZSTACK(:, :)
            integer(kind=8) :: ylstack_l
            {'integer(kind=8) :: ylstack_u' if check_bounds else ''}

            {'istsz = nlon+nlon*nz+nclv*nlon' if nclv_param else 'istsz = 3*nlon+nlon*nz+2'}
            ALLOCATE(ZSTACK(ISTSZ, nb))
            """
        else:
            fcode_stack_decl = f"""
            integer :: istsz
            REAL(KIND=JPRB), ALLOCATABLE :: ZSTACK(:, :)
            integer(kind=8) :: ylstack_l
            {'integer(kind=8) :: ylstack_u' if check_bounds else ''}

            {'istsz = nlon+nlon*nz+nclv*nlon' if nclv_param else 'istsz = 3*nlon+nlon*nz+2*max(c_sizeof(real(1,kind=jprb)), 8)/c_sizeof(real(1,kind=jprb))'}
            ALLOCATE(ZSTACK(ISTSZ, nb))
            """
    if cray_ptr_loc_rhs:
        fcode_stack_assign = """
            ylstack_l = 1
            ylstack_u = ylstack_l + istsz
        """
    else:
        fcode_stack_assign = """
            ylstack_l = loc(zstack(1, b))
            ylstack_u = ylstack_l + c_sizeof(real(1, kind=jprb)) * istsz
        """
    fcode_stack_dealloc = "DEALLOCATE(ZSTACK)"

    fcode_driver = f"""
subroutine driver(NLON, NZ, NB, FIELD1, FIELD2)
    {fcode_iso_c_binding if not generate_driver_stack else ''}
    use kernel_mod, only: kernel
    implicit none
    INTEGER, PARAMETER :: JPRB = SELECTED_REAL_KIND(13,300)
    INTEGER, INTENT(IN) :: NLON, NZ, NB
    real(kind=jprb), intent(inout) :: field1(nlon, nb)
    real(kind=jprb), intent(inout) :: field2(nlon, nz, nb)
    integer :: b
    {fcode_stack_decl if not generate_driver_stack else ''}
    do b=1,nb
        {fcode_stack_assign if not generate_driver_stack else ''}
        call KERNEL(1, nlon, nlon, nz, {'2, ' if not nclv_param else ''} field1(:,b), field2(:,:,b))
    end do
    {fcode_stack_dealloc if not generate_driver_stack else ''}
end subroutine driver
    """.strip()
    fcode_kernel = f"""
module kernel_mod
    implicit none
contains
    subroutine kernel(start, end, klon, klev, {'nclv, ' if not nclv_param else ''} field1, field2)
        use, intrinsic :: iso_c_binding, only : c_size_t
        implicit none
        integer, parameter :: jprb = selected_real_kind(13,300)
        {fcode_nclv_param if nclv_param else 'integer, intent(in) :: nclv'}
        integer, intent(in) :: start, end, klon, klev
        real(kind=jprb), intent(inout), target :: field1(klon)
        real(kind=jprb), intent(inout) :: field2(klon,klev)
        real(kind=jprb) :: tmp1(klon)
        real(kind=jprb) :: tmp2(klon, klev)
        real(kind=jprb) :: tmp3(nclv), tmp4(2), tmp5(klon, nclv)
        real(kind=jprb), pointer :: tmp3_ptr(:)
        integer :: jk, jl, jm

        tmp3_ptr => field1

        do jk=1,klev
            tmp1(jl) = 0.0_jprb
            do jl=start,end
                tmp2(jl, jk) = field2(jl, jk)
                tmp1(jl) = field2(jl, jk)
            end do
            field1(jl) = tmp1(jl)
        end do

        do jm=1,nclv
           tmp3(jm) = 0._jprb
           do jl=start,end
             tmp5(jl, jm) = field1(jl)
           enddo
        enddo
    end subroutine kernel
end module kernel_mod
    """.strip()

    (tmp_path/'driver.F90').write_text(fcode_driver)
    (tmp_path/'kernel_mod.F90').write_text(fcode_kernel)

    config = {
        'default': {
            'mode': 'idem',
            'role': 'kernel',
            'expand': True,
            'strict': True,
            'enable_imports': True,
        },
        'routines': {
            'driver': {'role': 'driver'}
        }
    }

    if frontend == FP and not generate_driver_stack:
        # Patch "LOC" intrinsic into fparser. This is not strictly needed (it will just represent it
        # as Array instead of an InlineCall) but makes for a more coherent check further down
        from fparser.two import Fortran2003  # pylint: disable=import-outside-toplevel
        Fortran2003.Intrinsic_Name.other_inquiry_names.update({"LOC": {'min': 1, 'max': 1}})
        Fortran2003.Intrinsic_Name.generic_function_names.update({"LOC": {'min': 1, 'max': 1}})
        Fortran2003.Intrinsic_Name.function_names += ["LOC"]

    scheduler = Scheduler(
        paths=[tmp_path], config=SchedulerConfig.from_dict(config),
        frontend=frontend, xmods=[tmp_path]
    )

    transformation = TemporariesPoolAllocatorTransformation(
        block_dim=block_dim, horizontal=horizontal, check_bounds=check_bounds,
        cray_ptr_loc_rhs=cray_ptr_loc_rhs
    )
    scheduler.process(transformation=transformation)
    kernel_item = scheduler['kernel_mod#kernel']

    assert transformation._key in kernel_item.trafo_data

    # set kind comaprison string
    if frontend == OMNI:
        kind_real = 'selected_real_kind(13, 300)'
    else:
        kind_real = 'jprb'

    if nclv_param:
        if frontend == OMNI:
            trafo_data_compare = (
                f'3 * c_sizeof(real(1, kind={kind_real})) * klon + '
                f'c_sizeof(real(1, kind={kind_real})) * klev * klon'
            )

            if generate_driver_stack:
                stack_size = (
                    f'3 * c_sizeof(real(1, kind={kind_real})) * nlon / '
                    'c_sizeof(real(1, kind=jprb)) '
                    f'+ c_sizeof(real(1, kind={kind_real})) * nlon * nz / '
                    'c_sizeof(real(1, kind=jprb))'
                )
            else:
                stack_size = '3 * nlon + nlon * nz'
        else:
            trafo_data_compare = (
                f'c_sizeof(real(1, kind={kind_real})) * klon + '
                f'c_sizeof(real(1, kind={kind_real})) * klev * klon '
                f'+ c_sizeof(real(1, kind={kind_real})) * klon * nclv'
            )

            if generate_driver_stack:
                stack_size = (
                    f'c_sizeof(real(1, kind={kind_real})) * nlon / c_sizeof(real(1, kind=jprb))'
                    f'+ c_sizeof(real(1, kind={kind_real})) * nlon * nz / '
                    'c_sizeof(real(1, kind=jprb))'
                    f'+ c_sizeof(real(1, kind={kind_real})) * nclv * nlon / '
                    'c_sizeof(real(1, kind=jprb))'
                )
            else:
                stack_size = 'nlon + nlon * nz + nclv * nlon'

    else:
        trafo_data_compare = (
            f'c_sizeof(real(1, kind={kind_real})) * klon + '
            f'c_sizeof(real(1, kind={kind_real})) * klev * klon '
            f'+ max(c_sizeof(real(1, kind={kind_real})), 8) * nclv '
            f'+ c_sizeof(real(1, kind={kind_real})) * klon * nclv'
        )

        if generate_driver_stack:
            stack_size = (
                f'3 * c_sizeof(real(1, kind={kind_real})) * nlon / '
                f'c_sizeof(real(1, kind=jprb))'
                f'+ c_sizeof(real(1, kind={kind_real})) * nlon * nz / '
                f'c_sizeof(real(1, kind=jprb))'
                f'+ 2 * max(c_sizeof(real(1, kind={kind_real})), 8) / '
                'c_sizeof(real(1, kind=jprb))'
            )
        else:
            stack_size = (
                '3 * nlon + nlon * nz '
                f'+ 2 * max(c_sizeof(real(1, kind={kind_real})), 8) / '
                f'c_sizeof(real(1, kind={kind_real}))'
            )

    trafo_data_compare = trafo_data_compare.replace(' ', '')
    stack_size = stack_size.replace(' ', '')
    if cray_ptr_loc_rhs:
        kind_real = kind_real.replace(' ', '')
        trafo_data_compare = trafo_data_compare.replace(f'max(c_sizeof(real(1,kind={kind_real})),8)*', '')
        trafo_data_compare = trafo_data_compare.replace(f'c_sizeof(real(1,kind={kind_real}))*', '')
        stack_size = remove_redundant_substrings(stack_size, kind_real)
        if stack_size[-2:] == "+2":
            # This is a little hacky but unless we start to properly assemble the size expression
            # symbolically, this is the easiest to fix the expression ordering
            stack_size = f"2+{stack_size[:-2]}"
    assert kernel_item.trafo_data[transformation._key]['stack_size'] == trafo_data_compare
    assert all(v.scope is None for v in
                               FindVariables().visit(kernel_item.trafo_data[transformation._key]['stack_size']))

    #
    # A few checks on the driver
    #
    driver = scheduler['#driver'].ir
    # Has c_sizeof procedure been imported?
    check_c_sizeof_import(driver)

    # Has the stack been added to the call statement?
    calls = FindNodes(CallStatement).visit(driver.body)
    assert len(calls) == 1
    if nclv_param:
        expected_args = ('1', 'nlon', 'nlon', 'nz', 'field1(:,b)', 'field2(:,:,b)')
    else:
        expected_args = ('1', 'nlon', 'nlon', 'nz', '2', 'field1(:,b)', 'field2(:,:,b)')
    if check_bounds:
        expected_kwargs = (('YDSTACK_L', 'ylstack_l'), ('YDSTACK_U', 'ylstack_u'))
    else:
        expected_kwargs = (('YDSTACK_L', 'ylstack_l'),)
    if cray_ptr_loc_rhs:
        if frontend == OMNI and not generate_driver_stack:
            # If the stack exists already in the driver, that variable is used. And because
            # OMNI lower-cases everything, this will result in a lower-case name for the
            # argument for that particular case...
            expected_kwargs += (('zstack', 'zstack(:,b)'),)
        else:
            expected_kwargs += (('ZSTACK', 'zstack(:,b)'),)
    assert calls[0].arguments == expected_args
    assert calls[0].kwarguments == expected_kwargs

    if generate_driver_stack:
        check_stack_created_in_driver(driver, stack_size, calls[0], 1, generate_driver_stack, check_bounds=check_bounds,
                cray_ptr_loc_rhs=cray_ptr_loc_rhs)
    else:
        check_stack_created_in_driver(driver, stack_size, calls[0], 1, generate_driver_stack, kind_real=kind_real,
                check_bounds=check_bounds, cray_ptr_loc_rhs=cray_ptr_loc_rhs)
    #
    # A few checks on the kernel
    #
    kernel = kernel_item.ir

    # Has c_sizeof procedure been imported?
    check_c_sizeof_import(kernel)

    # Has the stack been added to the arguments?
    # assert 'ydstack' in kernel.arguments
    assert 'ydstack_l' in kernel.arguments
    if check_bounds:
        assert 'ydstack_u' in kernel.arguments

    # Is it being assigned to a local variable?
    # assert 'ylstack' in kernel.variables
    assert 'ylstack_l' in kernel.variables
    if check_bounds:
        assert 'ylstack_u' in kernel.variables

    # Let's check for the relevant "allocations" happening in the right order
    if nclv_param:
        tmp_indices = (1, 2, 5)
    else:
        tmp_indices = (1, 2, 3, 5)
    assign_idx = {}
    for idx, assign in enumerate(FindNodes(Assignment).visit(kernel.body)):
        if assign.lhs == 'ylstack_l' and assign.rhs == 'ydstack_l':
            # Local copy of stack status
            assign_idx['stack_assign'] = idx
        elif str(assign.lhs).lower().startswith('ip_tmp') and assign.rhs == 'ylstack_l':
            # Assign Cray pointer for tmp1, tmp2, tmp5 (and tmp3, tmp4 if no alloc_dims provided)
            for tmp_index in tmp_indices:
                if f'ip_tmp{tmp_index}' == assign.lhs:
                    assign_idx[f'tmp{tmp_index}_ptr_assign'] = idx
        elif assign.lhs == 'ylstack_l' and 'ylstack_l' in assign.rhs and 'c_sizeof' in assign.rhs:
            _size = str(assign.rhs).lower().replace(f'*max(c_sizeof(real(1, kind={kind_real})), 8)', '')
            _size = _size.replace(f'*c_sizeof(real(1, kind={kind_real}))', '')
            _size = _size.replace('ylstack_l + ', '')

            # Stack increment for tmp1, tmp2, tmp5 (and tmp3, tmp4 if no alloc_dims provided)
            for tmp_index in tmp_indices:

                dim = f"{kernel.variable_map[f'tmp{tmp_index}'].shape[0]}"
                for v in kernel.variable_map[f'tmp{tmp_index}'].shape[1:]:
                    dim += f'*{v}'

                if dim == _size:
                    assign_idx[f'tmp{tmp_index}_stack_incr'] = idx

    expected_assign_in_order = ['stack_assign']
    if not cray_ptr_loc_rhs:
        for tmp_index in tmp_indices:
            expected_assign_in_order += [f'tmp{tmp_index}_ptr_assign', f'tmp{tmp_index}_stack_incr']
        assert set(expected_assign_in_order) == set(assign_idx.keys())

    for assign1, assign2 in zip(expected_assign_in_order, expected_assign_in_order[1:]):
        assert assign_idx[assign2] > assign_idx[assign1]

    # Check for pointer declarations in generated code
    fcode = kernel.to_fortran()
    for tmp_index in tmp_indices:
        assert f'pointer(ip_tmp{tmp_index}, tmp{tmp_index})' in fcode.lower()

    # Check for stack size safeguards in generated code
    if check_bounds:
        assert fcode.lower().count('if (ylstack_l > ylstack_u)') == len(tmp_indices)
        assert fcode.lower().count('stop') == len(tmp_indices)
    else:
        assert 'if (ylstack_l > ylstack_u)' not in fcode.lower()
        assert 'stop' not in fcode.lower()


@pytest.mark.parametrize('frontend', available_frontends())
@pytest.mark.parametrize('directive', [None, 'openmp', 'openacc'])
@pytest.mark.parametrize('stack_insert_pragma', [False, True])
@pytest.mark.parametrize('cray_ptr_loc_rhs', [False, True])
def test_pool_allocator_temporaries_kernel_sequence(tmp_path, frontend, block_dim, directive,
                                                    stack_insert_pragma, cray_ptr_loc_rhs, horizontal):
    if directive == 'openmp':
        driver_loop_pragma1 = '!$omp parallel default(shared) private(b) firstprivate(a)\n    !$omp do'
        driver_end_loop_pragma1 = '!$omp end do\n    !$omp end parallel'
        driver_loop_pragma2 = '!$omp parallel do firstprivate(a)'
        driver_end_loop_pragma2 = '!$omp end parallel do'
        kernel_pragma = ''
    elif directive == 'openacc':
        driver_loop_pragma1 = '!$acc parallel loop gang private(b) firstprivate(a)'
        driver_end_loop_pragma1 = '!$acc end parallel loop'
        driver_loop_pragma2 = '!$acc parallel loop gang firstprivate(a)'
        driver_end_loop_pragma2 = '!$acc end parallel loop'
        kernel_pragma = '!$acc routine vector'
    else:
        driver_loop_pragma1 = ''
        driver_end_loop_pragma1 = ''
        driver_loop_pragma2 = ''
        driver_end_loop_pragma2 = ''
        kernel_pragma = ''

    if stack_insert_pragma:
        stack_size_location_pragma = '!$loki stack-insert'
    else:
        stack_size_location_pragma = ''


    fcode_parkind_mod = """
module parkind1
implicit none
integer, parameter :: jprb = selected_real_kind(13,300)
integer, parameter :: jpim = selected_int_kind(9)
integer, parameter :: jplm = jpim
end module parkind1
    """.strip()

    fcode_driver = f"""
subroutine driver(NLON, NZ, NB, FIELD1, FIELD2)
    use kernel_mod, only: kernel, kernel2
    implicit none
    INTEGER, PARAMETER :: JPRB = SELECTED_REAL_KIND(13,300)
    INTEGER, INTENT(IN) :: NLON, NZ, NB
    real(kind=jprb), intent(inout) :: field1(nlon, nb)
    real(kind=jprb), intent(inout) :: field2(nlon, nz, nb)
    integer :: a,b

    ! a = 1, necessary to check loki stack-insert pragma
    a = 1
    {stack_size_location_pragma}

    {driver_loop_pragma1}
    do b=1,nb
        call KERNEL(1, nlon, nlon, nz, field1(:,b), field2(:,:,b))
    end do
    {driver_end_loop_pragma1}

    {driver_loop_pragma2}
    do b=1,nb
        call KERNEL2(1, nlon, nlon, nz, field2(:,:,b))
    end do
    {driver_end_loop_pragma2}
end subroutine driver
    """.strip()

    fcode_kernel = f"""
module kernel_mod
    implicit none
contains
    subroutine kernel(start, end, klon, klev, field1, field2)
        use parkind1, only: jprb, jpim, jplm
        implicit none
        integer, intent(in) :: start, end, klon, klev
        real(kind=jprb), intent(inout) :: field1(klon)
        real(kind=jprb), intent(inout) :: field2(klon,klev)
        real(kind=jprb) :: tmp1(klon)
        real(kind=jprb) :: tmp2(klon, klev)
        integer(kind=jpim) :: tmp3(klon*2)
        logical(kind=jplm) :: tmp4(klev)
        integer :: jk, jl
        {kernel_pragma}

        do jk=1,klev
            tmp1(jl) = 0.0_jprb
            do jl=start,end
                tmp2(jl, jk) = field2(jl, jk)
                tmp1(jl) = field2(jl, jk)
            end do
            field1(jl) = tmp1(jl)
            tmp4(jk) = .true.
        end do

        do jl=start,end
           tmp3(jl) = 1_jpim
           tmp3(jl+klon) = 1_jpim
        enddo
    end subroutine kernel

    subroutine kernel2(start, end, klon, klev, field2)
        implicit none
        integer, parameter :: jprb = selected_real_kind(13,300)
        integer, intent(in) :: start, end, klon, klev
        real(kind=jprb), intent(inout) :: field2(klon,klev)
        real(kind=jprb) :: tmp1(2*klon, klev), tmp2(klon, 0:klev)
        integer :: jk, jl

        do jk=1,klev
            do jl=start,end
                tmp1(jl, jk) = field2(jl, jk)
                tmp1(jl+klon, jk) = field2(jl, jk)*2._jprb
                tmp2(jl, jk) = tmp1(jl, jk) + 1._jprb
                field2(jl, jk) = tmp2(jl, jk)
            end do
        end do
    end subroutine kernel2

end module kernel_mod
    """.strip()

    (tmp_path/'driver.F90').write_text(fcode_driver)
    (tmp_path/'kernel_mod.F90').write_text(fcode_kernel)
    (tmp_path/'parkind_mod.F90').write_text(fcode_parkind_mod)

    config = {
        'default': {
            'mode': 'idem',
            'role': 'kernel',
            'expand': True,
            'strict': True,
            'enable_imports': True,
        },
        'routines': {
            'driver': {'role': 'driver'}
        }
    }
    scheduler = Scheduler(
        paths=[tmp_path], config=SchedulerConfig.from_dict(config),
        frontend=frontend, xmods=[tmp_path]
    )

    transformation = TemporariesPoolAllocatorTransformation(
        block_dim=block_dim, horizontal=horizontal, directive=directive, cray_ptr_loc_rhs=cray_ptr_loc_rhs
    )
    scheduler.process(transformation=transformation)
    kernel_item = scheduler['kernel_mod#kernel']
    kernel2_item = scheduler['kernel_mod#kernel2']

    # set kind comaprison string
    if frontend == OMNI:
        kind_real = 'selected_real_kind(13, 300)'
        kind_int = '4'
        kind_log = '4'
    else:
        kind_real = 'jprb'
        kind_int = 'jpim'
        kind_log = 'jplm'

    tsize_real = f'c_sizeof(real(1, kind={kind_real}))'
    tsize_int = f'c_sizeof(int(1, kind={kind_int}))'
    tsize_log = f'max(c_sizeof(logical(true, kind={kind_log})), 8)'

    assert transformation._key in kernel_item.trafo_data
    if cray_ptr_loc_rhs:
        exp_stack_size = '3*klon + klev*klon + klev'
    else:
        exp_stack_size = f'{tsize_real}*klon + {tsize_real}*klev*klon + 2*{tsize_int}*klon + {tsize_log}*klev'
    assert kernel_item.trafo_data[transformation._key]['stack_size'] == exp_stack_size
    if cray_ptr_loc_rhs:
        exp_stack_size = '3*klev*klon + klon'
    else:
        exp_stack_size = f'3*{tsize_real}*klev*klon + {tsize_real}*klon'
    assert kernel2_item.trafo_data[transformation._key]['stack_size'] == exp_stack_size
    assert all(
        v.scope is None
        for v in FindVariables().visit(kernel_item.trafo_data[transformation._key]['stack_size'])
    )
    assert all(
        v.scope is None
        for v in FindVariables().visit(kernel2_item.trafo_data[transformation._key]['stack_size'])
    )

    #
    # A few checks on the driver
    #
    driver = scheduler['#driver'].ir

    stack_order = FindNodes(Assignment).visit(driver.body)
    if stack_insert_pragma:
        assert stack_order[0].lhs == "a"
    else:
        assert stack_order[0].lhs == "ISTSZ"

    # Check if allocation type symbols have been imported
    if frontend != OMNI:
        assert 'jpim' in driver.imported_symbols
        assert 'jplm' in driver.imported_symbols
        assert driver.import_map['jpim'] == driver.import_map['jplm']
        assert 'jprb' not in driver.import_map['jpim'].symbols

    # Has the stack been added to the call statements?
    calls = FindNodes(CallStatement).visit(driver.body)
    expected_kwarguments = (('YDSTACK_L', 'ylstack_l'), ('YDSTACK_U', 'ylstack_U'))
    if cray_ptr_loc_rhs:
        expected_kwarguments += (('ZSTACK', 'zstack(:,b)'),)
    assert len(calls) == 2
    assert calls[0].arguments == ('1', 'nlon', 'nlon', 'nz', 'field1(:,b)', 'field2(:,:,b)')
    assert calls[0].kwarguments == expected_kwarguments
    assert calls[1].arguments == ('1', 'nlon', 'nlon', 'nz', 'field2(:,:,b)')
    assert calls[1].kwarguments == expected_kwarguments

    stack_size = f'max({tsize_real}*nlon + {tsize_real}*nlon*nz + '
    stack_size += f'2*{tsize_int}*nlon + {tsize_log}*nz,'
    stack_size += f'3*{tsize_real}*nlon*nz + {tsize_real}*nlon)/' \
                  'c_sizeof(real(1, kind=jprb))'
    if cray_ptr_loc_rhs:
        stack_size = 'max(3*nlon + nlon*nz + nz, 3*nlon*nz + nlon)'

    check_stack_created_in_driver(driver, stack_size, calls[0], 2, cray_ptr_loc_rhs=cray_ptr_loc_rhs)

    # Has the data sharing been updated?
    if directive in ['openmp', 'openacc']:
        keyword = {'openmp': 'omp', 'openacc': 'acc'}[directive]
        pragmas = [
            p for p in FindNodes(Pragma).visit(driver.body)
            if p.keyword.lower() == keyword and p.content.startswith('parallel')
        ]
        assert len(pragmas) == 2
        for pragma in pragmas:
            parameters = get_pragma_parameters(pragma, starts_with='parallel', only_loki_pragmas=False)
            assert 'private' in parameters and 'ylstack' in parameters['private'].lower()
            assert not 'ylstack' in parameters['firstprivate'].lower()

    # Are there data regions for the stack?
    if directive == ['openacc']:
        pragmas = [
            p for p in FindNodes(Pragma).visit(driver.body)
            if p.keyword.lower() == 'acc' and 'data' in p.content
        ]
        assert len(pragmas) == 2
        parameters = get_pragma_parameters(pragmas[0], starts_with='data', only_loki_pragmas=False)
        assert parameters['create'] == 'zstack'

    #
    # A few checks on the kernel
    #
    for count, item in enumerate([kernel_item, kernel2_item]):
        kernel = item.ir

        # Has the stack been added to the arguments?
        assert 'ydstack_l' in kernel.arguments
        assert 'ydstack_u' in kernel.arguments

        # Is it being assigned to a local variable?
        assert 'ylstack_l' in kernel.variables
        assert 'ylstack_u' in kernel.variables

        dim1 = f"{kernel.variable_map['tmp1'].shape[0]}"
        for v in kernel.variable_map['tmp1'].shape[1:]:
            dim1 += f'*{v}'
        dim2 = f"{kernel.variable_map['tmp2'].shape[0]}"
        for v in kernel.variable_map['tmp2'].shape[1:]:
            dim2 += f'*{v}'

        # Let's check for the relevant "allocations" happening in the right order
        assign_idx = {}
        for idx, ass in enumerate(FindNodes(Assignment).visit(kernel.body)):
            _size = str(ass.rhs).lower().replace(f'*c_sizeof(real(1, kind={kind_real}))', '')
            _size = _size.replace(f'*c_sizeof(int(1, kind={kind_int}))', '')
            _size = _size.replace(f'*max(c_sizeof(logical(.true., kind={kind_log})), 8)', '')
            _size = _size.replace('ylstack_l + ', '')

            if ass.lhs == 'ylstack_l' and ass.rhs == 'ydstack_l':
                # Local copy of stack status
                assign_idx['stack_assign'] = idx
            elif ass.lhs == 'ylstack_u' and ass.rhs == 'ydstack_u':
                # Local copy of stack status
                assign_idx['stack_assign_end'] = idx
            elif ass.lhs == 'ip_tmp1' and ass.rhs == 'ylstack_l':
                # ass Cray pointer for tmp1
                assign_idx['tmp1_ptr_assign'] = idx
            elif ass.lhs == 'ip_tmp2' and ass.rhs == 'ylstack_l':
                # ass Cray pointer for tmp2
                assign_idx['tmp2_ptr_assign'] = idx
            elif ass.lhs == 'ylstack_l' and 'ylstack_l' in ass.rhs and 'c_sizeof' in ass.rhs and dim1 == _size:
                # Stack increment for tmp1
                assign_idx['tmp1_stack_incr'] = idx
            elif ass.lhs == 'ylstack_l' and 'ylstack_l' in ass.rhs and 'c_sizeof' in ass.rhs:
                # Stack increment for tmp2
                assign_idx['tmp2_stack_incr'] = idx

        expected_assign_in_order = [
            'stack_assign', 'stack_assign_end', 'tmp1_ptr_assign', 'tmp1_stack_incr', 'tmp2_ptr_assign',
            'tmp2_stack_incr'
        ]
        if not cray_ptr_loc_rhs:
            assert set(expected_assign_in_order) == set(assign_idx.keys())

            for assign1, assign2 in zip(expected_assign_in_order, expected_assign_in_order[1:]):
                assert assign_idx[assign2] > assign_idx[assign1]

        # Check for pointer declarations in generated code
        fcode = kernel.to_fortran()
        assert 'pointer(ip_tmp1, tmp1)' in fcode.lower()
        assert 'pointer(ip_tmp2, tmp2)' in fcode.lower()

        # Check for stack size safegurads in generated code
        if count == 0:
            assert fcode.lower().count('if (ylstack_l > ylstack_u)') == 4
            assert fcode.lower().count('stop') == 4
        else:
            assert fcode.lower().count('if (ylstack_l > ylstack_u)') == 2
            assert fcode.lower().count('stop') == 2


@pytest.mark.parametrize('frontend', available_frontends())
@pytest.mark.parametrize('directive', [None, 'openmp', 'openacc'])
@pytest.mark.parametrize('cray_ptr_loc_rhs', [False, True])
def test_pool_allocator_temporaries_kernel_nested(tmp_path, frontend, block_dim, directive, cray_ptr_loc_rhs):
    if directive == 'openmp':
        driver_pragma = '!$omp PARALLEL do PRIVATE(b)'
        driver_end_pragma = '!$omp end parallel do'
        kernel_pragma = ''
    elif directive == 'openacc':
        driver_pragma = '!$acc parallel loop gang'
        driver_end_pragma = '!$acc end parallel loop'
        kernel_pragma = '!$acc routine vector'
    else:
        driver_pragma = ''
        driver_end_pragma = ''
        kernel_pragma = ''

    fcode_parkind_mod = """
module parkind1
implicit none
integer, parameter :: jwrb = selected_real_kind(13,300)
integer, parameter :: jpim = selected_int_kind(9)
integer, parameter :: jplm = jpim
end module parkind1
    """.strip()

    fcode_driver = f"""
subroutine driver(NLON, NZ, NB, FIELD1, FIELD2)
    use kernel_mod, only: kernel
    use parkind1, only : jpim
    implicit none
    INTEGER, PARAMETER :: JWRB = SELECTED_REAL_KIND(13,300)
    INTEGER, INTENT(IN) :: NLON, NZ, NB
    real(kind=jwrb), intent(inout) :: field1(nlon, nb)
    real(kind=jwrb), intent(inout) :: field2(nlon, nz, nb)
    integer :: b
    {driver_pragma}
    do b=1,nb
        call KERNEL(1, nlon, nlon, nz, field1(:,b), field2(:,:,b))
    end do
    {driver_end_pragma}
end subroutine driver
    """.strip()
    fcode_kernel = f"""
module kernel_mod
    implicit none
contains
    subroutine kernel(start, end, klon, klev, field1, field2)
        use parkind1, only : jpim, jplm
        implicit none
        integer, parameter :: jwrb = selected_real_kind(13,300)
        integer, intent(in) :: start, end, klon, klev
        real(kind=jwrb), intent(inout) :: field1(klon)
        real(kind=jwrb), intent(inout) :: field2(klon,klev)
        real(kind=jwrb) :: tmp1(klon)
        real(kind=jwrb) :: tmp2(klon, klev)
        integer(kind=jpim) :: tmp3(klon*2)
        logical(kind=jplm) :: tmp4(klev)
        integer :: jk, jl
        {kernel_pragma}

        do jk=1,klev
            tmp1(jl) = 0.0_jwrb
            do jl=start,end
                tmp2(jl, jk) = field2(jl, jk)
                tmp1(jl) = field2(jl, jk)
            end do
            field1(jl) = tmp1(jl)
            tmp4(jk) = .true.
        end do

        do jl=start,end
           tmp3(jl) = 1_jpim
           tmp3(jl+klon) = 1_jpim
        enddo

        call kernel2(start, end, klon, klev, field2)
    end subroutine kernel

    subroutine kernel2(start, end, columns, levels, field2)
        implicit none
        integer, parameter :: jwrb = selected_real_kind(13,300)
        integer, intent(in) :: start, end, columns, levels
        real(kind=jwrb), intent(inout) :: field2(columns,levels)
        real(kind=jwrb) :: tmp1(2*columns, levels), tmp2(columns, levels)
        integer :: jk, jl
        {kernel_pragma}

        do jk=1,levels
            do jl=start,end
                tmp1(jl, jk) = field2(jl, jk)
                tmp1(jl+columns, jk) = field2(jl, jk)*2._jwrb
                tmp2(jl, jk) = tmp1(jl, jk) + 1._jwrb
                field2(jl, jk) = tmp2(jl, jk)
            end do
        end do
    end subroutine kernel2

end module kernel_mod
    """.strip()

    (tmp_path/'driver.F90').write_text(fcode_driver)
    (tmp_path/'kernel_mod.F90').write_text(fcode_kernel)
    (tmp_path/'parkind_mod.F90').write_text(fcode_parkind_mod)

    config = {
        'default': {
            'mode': 'idem',
            'role': 'kernel',
            'expand': True,
            'strict': True,
            'enable_imports': True,
        },
        'routines': {
            'driver': {'role': 'driver', 'real_kind': 'jwrb'}
        }
    }

    scheduler = Scheduler(
        paths=[tmp_path], config=SchedulerConfig.from_dict(config),
        frontend=frontend, xmods=[tmp_path]
    )

    transformation = TemporariesPoolAllocatorTransformation(block_dim=block_dim,
                                                            directive=directive, cray_ptr_loc_rhs=cray_ptr_loc_rhs)
    scheduler.process(transformation=transformation)
    kernel_item = scheduler['kernel_mod#kernel']
    kernel2_item = scheduler['kernel_mod#kernel2']

    # set kind comaprison string
    if frontend == OMNI:
        kind_real = 'selected_real_kind(13, 300)'
        kind_int = '4'
        kind_log = '4'
    else:
        kind_real = 'jwrb'
        kind_int = 'jpim'
        kind_log = 'jplm'

    tsize_real = f'max(c_sizeof(real(1, kind={kind_real})), 8)'
    tsize_int = f'max(c_sizeof(int(1, kind={kind_int})), 8)'
    tsize_log = f'max(c_sizeof(logical(true, kind={kind_log})), 8)'

    assert transformation._key in kernel_item.trafo_data
    if cray_ptr_loc_rhs:
        exp_stack_size = '3*klon + 4*klev*klon + klev'
    else:
        exp_stack_size = f'{tsize_real}*klon + 4*{tsize_real}*klev*klon + 2*{tsize_int}*klon + {tsize_log}*klev'
    assert kernel_item.trafo_data[transformation._key]['stack_size'] == exp_stack_size
    if cray_ptr_loc_rhs:
        exp_stack_size = '3*columns*levels'
    else:
        exp_stack_size = f'3*{tsize_real}*columns*levels'
    assert kernel2_item.trafo_data[transformation._key]['stack_size'] == exp_stack_size
    assert all(
        v.scope is None
        for v in FindVariables().visit(kernel_item.trafo_data[transformation._key]['stack_size'])
    )
    assert all(
        v.scope is None
        for v in FindVariables().visit(kernel2_item.trafo_data[transformation._key]['stack_size'])
    )

    #
    # A few checks on the driver
    #
    driver = scheduler['#driver'].ir

    # Check if allocation type symbols have been imported
    if frontend != OMNI:
        assert 'jpim' in driver.imported_symbols
        assert 'jplm' in driver.imported_symbols
        assert driver.import_map['jpim'] == driver.import_map['jplm']

    # Has the stack been added to the call statements?
    calls = FindNodes(CallStatement).visit(driver.body)
    expected_kwarguments = (('YDSTACK_L', 'ylstack_l'), ('YDSTACK_U', 'ylstack_u'))
    if cray_ptr_loc_rhs:
        expected_kwarguments += (('ZSTACK', 'zstack(:,b)'),)
    assert len(calls) == 1
    assert calls[0].arguments == ('1', 'nlon', 'nlon', 'nz', 'field1(:,b)', 'field2(:,:,b)')
    assert calls[0].kwarguments == expected_kwarguments

    stack_size = f'{tsize_real}*nlon/c_sizeof(real(1, kind=jwrb)) +'
    stack_size += f'4*{tsize_real}*nlon*nz/c_sizeof(real(1, kind=jwrb)) +'
    stack_size += f'2*{tsize_int}*nlon/c_sizeof(real(1, kind=jwrb)) +'
    stack_size += f'{tsize_log}*nz/c_sizeof(real(1, kind=jwrb))'
    if cray_ptr_loc_rhs:
        stack_size = '3*nlon + 4*nlon*nz + nz'
    check_stack_created_in_driver(
        driver, stack_size, calls[0], 1, kind_real='jwrb', simplify_stmt=True,
        cray_ptr_loc_rhs=cray_ptr_loc_rhs
    )

    # check if stack allocatable in the driver has the correct kind parameter
    if not frontend == OMNI:
        assert driver.symbol_map['zstack'].type.kind == 'jwrb'

    # Has the data sharing been updated?
    if directive in ['openmp', 'openacc']:
        keyword = {'openmp': 'omp', 'openacc': 'acc'}[directive]
        pragmas = [
            p for p in FindNodes(Pragma).visit(driver.body)
            if p.keyword.lower() == keyword and p.content.startswith('parallel')
        ]
        assert len(pragmas) == 1
        for pragma in pragmas:
            parameters = get_pragma_parameters(pragma, starts_with='parallel', only_loki_pragmas=False)
            assert 'private' in parameters and 'ylstack' in parameters['private'].lower()
            if directive == 'openmp':
                assert 'b' in parameters['private']

    # Are there data regions for the stack?
    if directive == ['openacc']:
        pragmas = [
            p for p in FindNodes(Pragma).visit(driver.body)
            if p.keyword.lower() == 'acc' and 'data' in p.content
        ]
        assert len(pragmas) == 2
        parameters = get_pragma_parameters(pragmas[0], starts_with='data', only_loki_pragmas=False)
        assert parameters['create'] == 'zstack'

    #
    # A few checks on the kernels
    #
    calls = FindNodes(CallStatement).visit(kernel_item.ir.body)
    expected_kwarguments = (('YDSTACK_L', 'ylstack_l'), ('YDSTACK_U', 'ylstack_u'))
    if cray_ptr_loc_rhs:
        expected_kwarguments += (('ZSTACK', 'zstack'),)
    assert len(calls) == 1
    assert calls[0].arguments == ('start', 'end', 'klon', 'klev', 'field2')
    assert calls[0].kwarguments == expected_kwarguments

    for count, item in enumerate([kernel_item, kernel2_item]):
        kernel = item.ir

        # Has the stack been added to the arguments?
        assert 'ydstack_l' in kernel.arguments
        assert 'ydstack_u' in kernel.arguments

        # Is it being assigned to a local variable?
        assert 'ylstack_l' in kernel.variables
        assert 'ylstack_u' in kernel.variables

        dim1 = f"{kernel.variable_map['tmp1'].shape[0]}"
        for v in kernel.variable_map['tmp1'].shape[1:]:
            dim1 += f'*{v}'
        dim2 = f"{kernel.variable_map['tmp2'].shape[0]}"
        for v in kernel.variable_map['tmp2'].shape[1:]:
            dim2 += f'*{v}'

        # Let's check for the relevant "allocations" happening in the right order
        assign_idx = {}
        for idx, ass in enumerate(FindNodes(Assignment).visit(kernel.body)):
            _size = str(ass.rhs).lower().replace(f'*max(c_sizeof(real(1, kind={kind_real})), 8)', '')
            _size = _size.replace(f'*max(c_sizeof(int(1, kind={kind_int})), 8)', '')
            _size = _size.replace(f'*max(c_sizeof(logical(.true., kind={kind_log})), 8)', '')
            _size = _size.replace('ylstack_l + ', '')

            if ass.lhs == 'ylstack_l' and ass.rhs == 'ydstack_l':
                # Local copy of stack status
                assign_idx['stack_assign'] = idx
            if ass.lhs == 'ylstack_u' and ass.rhs == 'ydstack_u':
                # Local copy of stack status
                assign_idx['stack_assign_end'] = idx
            elif ass.lhs == 'ip_tmp1' and ass.rhs == 'ylstack_l':
                # ass Cray pointer for tmp1
                assign_idx['tmp1_ptr_assign'] = idx
            elif ass.lhs == 'ip_tmp2' and ass.rhs == 'ylstack_l':
                # ass Cray pointer for tmp2
                assign_idx['tmp2_ptr_assign'] = idx
            elif ass.lhs == 'ylstack_l' and 'ylstack_l' in ass.rhs and 'c_sizeof' in ass.rhs and dim1 == _size:
                # Stack increment for tmp1
                assign_idx['tmp1_stack_incr'] = idx
            elif ass.lhs == 'ylstack_l' and 'ylstack_l' in ass.rhs and 'c_sizeof' in ass.rhs and dim2 == _size:
                # Stack increment for tmp2
                assign_idx['tmp2_stack_incr'] = idx

        expected_assign_in_order = [
            'stack_assign', 'stack_assign_end', 'tmp1_ptr_assign', 'tmp1_stack_incr', 'tmp2_ptr_assign',
            'tmp2_stack_incr'
        ]
        if not cray_ptr_loc_rhs:
            assert set(expected_assign_in_order) == set(assign_idx.keys())

            for assign1, assign2 in zip(expected_assign_in_order, expected_assign_in_order[1:]):
                assert assign_idx[assign2] > assign_idx[assign1]

        # Check for pointer declarations in generated code
        fcode = kernel.to_fortran()
        assert 'pointer(ip_tmp1, tmp1)' in fcode.lower()
        assert 'pointer(ip_tmp2, tmp2)' in fcode.lower()

        # Check for stack size safegurads in generated code
        if count == 0:
            assert fcode.lower().count('if (ylstack_l > ylstack_u)') == 4
            assert fcode.lower().count('stop') == 4
        else:
            assert fcode.lower().count('if (ylstack_l > ylstack_u)') == 2
            assert fcode.lower().count('stop') == 2


@pytest.mark.parametrize('frontend', available_frontends())
@pytest.mark.parametrize('cray_ptr_loc_rhs', [False, True])
def test_pool_allocator_more_call_checks(tmp_path, frontend, block_dim, caplog, cray_ptr_loc_rhs, horizontal):
    fcode = """
    module kernel_mod
      type point
         real :: x
         real :: y
         real :: z
      end type point
    contains
      real function inline_kernel(jl)
          integer, intent(in) :: jl
      end function inline_kernel
      subroutine optional_arg(klon, temp1, temp2)
          integer, intent(in) :: klon
          real, intent(inout) :: temp1
          real, intent(out), optional :: temp2
      end subroutine optional_arg
      subroutine kernel(start, end, klon, field1)
          implicit none

          interface
             real function inline_kernel(jl)
                 integer, intent(in) :: jl
             end function inline_kernel
          end interface

          integer, intent(in) :: start, end, klon
          real, intent(inout) :: field1(klon)
          real :: temp1(klon)
          real :: temp2(klon)
          type(point) :: p(klon)

          integer :: jl

          do jl=start,end
              field1(jl) = inline_kernel(jl)
              p(jl)%x = 0.
              p(jl)%y = 0.
              p(jl)%z = 0.
          end do

          call optional_arg(klon, temp1, temp2)
      end subroutine kernel
    end module kernel_mod
    """.strip()

    (tmp_path/'kernel.F90').write_text(fcode)

    config = {
        'default': {
            'mode': 'idem',
            'role': 'kernel',
            'expand': True,
            'strict': True,
            'enable_imports': True,
        },
        'routines': {
            'kernel': {}
        }
    }
    scheduler = Scheduler(
        paths=[tmp_path], config=SchedulerConfig.from_dict(config), frontend=frontend, xmods=[tmp_path]
    )

    transformation = TemporariesPoolAllocatorTransformation(block_dim=block_dim, horizontal=horizontal,
                                                            cray_ptr_loc_rhs=cray_ptr_loc_rhs)
    scheduler.process(transformation=transformation)
    item = scheduler['kernel_mod#kernel']
    kernel = item.ir

    # Has the stack been added to the arguments?
    assert 'ydstack_l' in kernel.arguments
    assert 'ydstack_u' in kernel.arguments

    # Is it being assigned to a local variable?
    assert 'ylstack_l' in kernel.variables
    assert 'ylstack_u' in kernel.variables

    # Has the stack been added to the call statement at the correct location?
    calls = FindNodes(CallStatement).visit(kernel.body)
    expected_kwarguments = (('YDSTACK_L', 'ylstack_l'), ('YDSTACK_U', 'ylstack_u'))
    if cray_ptr_loc_rhs:
        expected_kwarguments += (('ZSTACK', 'zstack'),)
    assert len(calls) == 1
    assert calls[0].arguments == ('klon', 'temp1', 'temp2')
    assert calls[0].kwarguments == expected_kwarguments

    if not frontend == OFP:
        # Now repeat the checks for the inline call
        calls = [i for i in FindInlineCalls().visit(kernel.body) if not i.name.lower() in ('max', 'c_sizeof', 'real')]
        if cray_ptr_loc_rhs:
            assert len(calls) == 2
            if calls[0].name == 'inline_kernel':
                relevant_call = calls[0]
            else:
                relevant_call = calls[1]
        else:
            assert len(calls) == 1
            relevant_call = calls[0]
        assert relevant_call.arguments == ('jl',)
        assert relevant_call.kwarguments == expected_kwarguments

    assert 'Derived-type vars in Subroutine:: kernel not supported in pool allocator' in caplog.text


@pytest.mark.parametrize('frontend', available_frontends())
@pytest.mark.parametrize('cray_ptr_loc_rhs', [False, True])
def test_pool_allocator_args_vs_kwargs(tmp_path, frontend, block_dim_alt, cray_ptr_loc_rhs, horizontal):
    fcode_parkind_mod = """
module parkind1
implicit none
integer, parameter :: jwrb = selected_real_kind(13,300)
integer, parameter :: jpim = selected_int_kind(9)
integer, parameter :: jplm = jpim
end module parkind1
    """.strip()

    fcode_module = """
module geom_mod
    implicit none
    type dim_type
       integer :: nb
    end type dim_type

    type geom_type
       type(dim_type) :: blk_dim
    end type geom_type

    integer :: n
end module geom_mod
"""

    fcode_driver = """
subroutine driver(NLON, NZ, GEOM, FIELD1, FIELD2)
    use kernel_mod, only: kernel, kernel2
    use parkind1, only : jpim
    use geom_mod, only : geom_type
    implicit none
    INTEGER, PARAMETER :: JWRB = SELECTED_REAL_KIND(13,300)
    INTEGER, INTENT(IN) :: NLON, NZ
    type(geom_type), intent(in) :: geom
    real(kind=jwrb), intent(inout) :: field1(nlon, geom%blk_dim%nb)
    real(kind=jwrb), intent(inout) :: field2(nlon, nz, geom%blk_dim%nb)
    integer :: b
    real(kind=jwrb) :: opt
    do b=1,geom%blk_dim%nb
        call KERNEL(start=1, end=nlon, klon=nlon, klev=nz, field1=field1(:,b), field2=field2(:,:,b))
        call KERNEL2(1, nlon, nlon, nz, field2=field2(:,:,b))
        call KERNEL2(1, nlon, nlon, nz, field2(:,:,b))
        call KERNEL2(1, nlon, nlon, nz, field2=field2(:,:,b), opt_arg=opt)
        call KERNEL2(1, nlon, nlon, nz, field2(:,:,b), opt)
    end do
end subroutine driver
    """.strip()

    fcode_kernel = """
module kernel_mod
    implicit none
contains
    subroutine kernel(start, end, klon, klev, field1, field2)
        use parkind1, only : jpim, jplm
        use geom_mod, only : n
        implicit none
        integer, parameter :: jwrb = selected_real_kind(13,300)
        integer, intent(in) :: start, end, klon, klev
        real(kind=jwrb), intent(inout) :: field1(klon)
        real(kind=jwrb), intent(inout) :: field2(klon,klev)
        real(kind=jwrb) :: tmp1(klon)
        real(kind=jwrb) :: tmp2(klon, klev)
        integer(kind=jpim) :: tmp3(klon*2)
        logical(kind=jplm) :: tmp4(klev)
        logical(kind=jplm) :: tmp5(klev,n)
        integer :: jk, jl

        do jk=1,klev
            tmp1(jl) = 0.0_jwrb
            do jl=start,end
                tmp2(jl, jk) = field2(jl, jk)
                tmp1(jl) = field2(jl, jk)
            end do
            field1(jl) = tmp1(jl)
            tmp4(jk) = .true.
            tmp5(jk,1:n) = .true.
        end do

        do jl=start,end
           tmp3(jl) = 1_jpim
           tmp3(jl+klon) = 1_jpim
        enddo

        call kernel2(start, end, klon, klev, field2)
    end subroutine kernel
    subroutine kernel2(start, end, columns, levels, field2, opt_arg)
        implicit none
        integer, parameter :: jwrb = selected_real_kind(13,300)
        integer, intent(in) :: start, end, columns, levels
        real(kind=jwrb), intent(inout) :: field2(columns,levels)
        real(kind=jwrb) :: tmp1(2*columns, levels), tmp2(columns, levels)
        real(kind=jwrb), optional :: opt_arg
        integer :: jk, jl

        do jk=1,levels
            do jl=start,end
                tmp1(jl, jk) = field2(jl, jk)
                tmp1(jl+columns, jk) = field2(jl, jk)*2._jwrb
                tmp2(jl, jk) = tmp1(jl, jk) + 1._jwrb
                field2(jl, jk) = tmp2(jl, jk)
            end do
        end do
    end subroutine kernel2

end module kernel_mod
    """.strip()

    (tmp_path / 'parkind1.F90').write_text(fcode_parkind_mod)
    (tmp_path / 'driver.F90').write_text(fcode_driver)
    (tmp_path / 'kernel.F90').write_text(fcode_kernel)
    (tmp_path / 'module.F90').write_text(fcode_module)

    config = {
        'default': {
            'mode': 'idem',
            'role': 'kernel',
            'expand': True,
            'strict': True,
            'ignore': ['parkind1'],
            'enable_imports': True,
        },
        'routines': {
            'driver': {'role': 'driver'}
        }
    }
    scheduler = Scheduler(
        paths=[tmp_path], config=SchedulerConfig.from_dict(config),
        frontend=frontend, xmods=[tmp_path]
    )

    transformation = TemporariesPoolAllocatorTransformation(block_dim=block_dim_alt, horizontal=horizontal,
                                                            cray_ptr_loc_rhs=cray_ptr_loc_rhs)
    scheduler.process(transformation=transformation)

    kernel = scheduler['kernel_mod#kernel'].ir
    kernel2 = scheduler['kernel_mod#kernel2'].ir
    driver = scheduler['#driver'].ir

    assert 'ydstack_l' in kernel.arguments
    assert 'ydstack_u' in kernel.arguments
    assert 'ydstack_l' in kernel2.arguments
    assert 'ydstack_u' in kernel2.arguments

    calls = FindNodes(CallStatement).visit(driver.body)
    additional_kwargs = (('ZSTACK', 'zstack(:,b)'),) if cray_ptr_loc_rhs else ()
    assert calls[0].arguments == ()
    assert calls[0].kwarguments == (
        ('start', 1), ('end', 'nlon'), ('klon', 'nlon'), ('klev', 'nz'),
        ('field1', 'field1(:, b)'), ('field2', 'field2(:, :, b)'),
        ('YDSTACK_L', 'YLSTACK_L'), ('YDSTACK_U', 'YLSTACK_U')
    ) + additional_kwargs
    assert calls[1].arguments == ('1', 'nlon', 'nlon', 'nz')
    assert calls[1].kwarguments == (
        ('field2', 'field2(:, :, b)'), ('YDSTACK_L', 'YLSTACK_L'), ('YDSTACK_U', 'YLSTACK_U')
    ) + additional_kwargs
    assert calls[2].arguments == ('1', 'nlon', 'nlon', 'nz', 'field2(:, :, b)')
    assert calls[2].kwarguments == (
            ('YDSTACK_L', 'YLSTACK_L'), ('YDSTACK_U', 'YLSTACK_U')
    ) + additional_kwargs
    assert calls[3].arguments == ('1', 'nlon', 'nlon', 'nz')
    assert calls[3].kwarguments == (
        ('field2', 'field2(:, :, b)'), ('opt_arg', 'opt'),
        ('YDSTACK_L', 'YLSTACK_L'), ('YDSTACK_U', 'YLSTACK_U')
    ) + additional_kwargs
    assert calls[4].arguments == ('1', 'nlon', 'nlon', 'nz', 'field2(:, :, b)', 'opt')
    assert calls[4].kwarguments == (
            ('YDSTACK_L', 'YLSTACK_L'), ('YDSTACK_U', 'YLSTACK_U')
    ) + additional_kwargs

    # check stack size allocation
    allocations = FindNodes(Allocation).visit(driver.body)
    assert len(allocations) == 1 and 'zstack(istsz,geom%blk_dim%nb)' in allocations[0].variables

    # check that array size was imported to the driver
    assert 'n' in driver.imported_symbols
