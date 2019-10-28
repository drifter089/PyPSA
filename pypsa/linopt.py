#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Tools for fast Linear Problem file writing. This module contains

- io functions for writing out variables, constraints and objective
  into a lp file.
- functions to create lp format based linear expression
- solver functions which read the lp file, run the problem and return the
  solution

This module supports the linear optimal power flow calculation whithout using
pyomo (see module linopt.py)
"""

import pandas as pd
import os, logging, re, io, subprocess
import numpy as np
from pandas import IndexSlice as idx

logger = logging.getLogger(__name__)

# =============================================================================
# writing functions
# =============================================================================

xCounter = 0
cCounter = 0
def reset_counter():
    global xCounter, cCounter
    xCounter, cCounter = 0, 0


def write_bound(n, lower, upper, axes=None):
    """
    Writer function for writing out mutliple variables at a time. If lower and
    upper are floats it demands to give pass axes, a tuple of (index, columns)
    or (index), for creating the variable of same upper and lower bounds.
    Return a series or frame with variable references.

    """
    axes = [axes] if isinstance(axes, pd.Index) else axes
    if axes is None:
        axes, shape = broadcasted_axes(lower, upper)
    else:
        shape = tuple(map(len, axes))
    ser_or_frame = pd.DataFrame if len(shape) > 1 else pd.Series
    length = np.prod(shape)
    global xCounter
    xCounter += length
    variables = np.array([f'x{x}' for x in range(xCounter - length, xCounter)],
                          dtype=object).reshape(shape)
    lower, upper = _str_array(lower), _str_array(upper)
    for s in (lower + ' <= '+ variables + ' <= '+ upper + '\n').flatten():
        n.bounds_f.write(s)
    return ser_or_frame(variables, *axes)

def write_constraint(n, lhs, sense, rhs, axes=None):
    """
    Writer function for writing out mutliple constraints to the corresponding
    constraints file. If lower and upper are numpy.ndarrays it axes must not be
    None but a tuple of (index, columns) or (index).
    Return a series or frame with constraint references.

    """
    axes = [axes] if isinstance(axes, pd.Index) else axes
    if axes is None:
        axes, shape = broadcasted_axes(lhs, rhs)
    else:
        shape = tuple(map(len, axes))
    ser_or_frame = pd.DataFrame if len(shape) > 1 else pd.Series
    length = np.prod(shape)
    global cCounter
    cCounter += length
    cons = np.array([f'c{x}' for x in range(cCounter - length, cCounter)],
                            dtype=object).reshape(shape)
    if isinstance(sense, str):
        sense = '=' if sense == '==' else sense
    lhs, sense, rhs = _str_array(lhs), _str_array(sense), _str_array(rhs)
    for c in (cons + ':\n' + lhs + sense + '\n' + rhs + '\n\n').flatten():
        n.constraints_f.write(c)
    return ser_or_frame(cons, *axes)


# =============================================================================
# helpers, helper functions
# =============================================================================

var_ref_suffix = '_varref' # after solving replace with '_opt'
con_ref_suffix = '_conref' # after solving replace with ''

def broadcasted_axes(*dfs):
    """
    Helper function which, from a collection of arrays, series, frames and other
    values, retrieves the axes of series and frames which result from
    broadcasting operations. It checks whether index and columns of given
    series and frames, repespectively, are aligned. Using this function allows
    to subsequently use pure numpy operations and keep the axes in the
    background.

    """
    axes = []
    shape = ()

    if set(map(type, dfs)) == {tuple}:
        dfs = sum(dfs, ())

    for df in dfs:
        if isinstance(df, (pd.Series, pd.DataFrame)):
            if len(axes):
                assert (axes[-1] == df.axes[-1]).all(), ('Series or DataFrames '
                       'are not aligned')
            axes = df.axes if len(df.axes) > len(axes) else axes
            shape = tuple(map(len, axes))
    return axes, shape


def linexpr(*tuples, as_pandas=False, return_axes=False):
    """
    Elementwise concatenation of tuples in the form (coefficient, variables).
    Coefficient and variables can be arrays, series or frames. Returns
    a np.ndarray of strings. If return_axes is set to True and a pd.Series or
    pd.DataFrame was past, the corresponding index (and column if existent) is
    returned additionaly.

    Parameters
    ----------
    tulples: tuple of tuples
        Each tuple must of the form (coeff, var), where
            * coeff is a numerical  value, or a numeical array, series, frame
            * var is a str or a array, series, frame of variable strings
    as_pandas : bool, default False
        Whether to return to resulting array as a series, if 1-dimensional, or
        a frame, if 2-dimensional. Supersedes return_axes argument.
    return_axes: Boolean, default False
        Whether to return index and column (if existent)

    Example
    -------
    Initialize coefficients and variables

    >>> coeff1 = 1
    >>> var1 = pd.Series(['a1', 'a2', 'a3'])
    >>> coeff2 = pd.Series([-0.5, -0.3, -1])
    >>> var2 = pd.Series(['b1', 'b2', 'b3'])

    Create the linear expression strings

    >>> linexpr((coeff1, var1), (coeff2, var2))
    array(['+1.0 a1 -0.5 b1', '+1.0 a2 -0.3 b2', '+1.0 a3 -1.0 b3'], dtype=object)

    For turning the result into a series or frame again:

    >>> linexpr((coeff1, var1), (coeff2, var2), as_pandas=True)
    0    +1.0 a1 -0.5 b1
    1    +1.0 a2 -0.3 b2
    2    +1.0 a3 -1.0 b3
    dtype: object

    For a further step the resulting frame can be used as the lhs of
    :func:`pypsa.linopt.write_contraint`

    """
    axes, shape = broadcasted_axes(*tuples)
    expr = np.repeat('', np.prod(shape)).reshape(shape).astype(object)
    if np.prod(shape):
        for coeff, var in tuples:
            expr += _str_array(coeff) + _str_array(var) + '\n'
    if as_pandas:
        twodims = len(shape) > 1
        return pd.DataFrame(expr, *axes) if twodims else pd.Series(expr, *axes)
    if return_axes:
        return (expr, *axes)
    return expr


def _str_array(array):
    if isinstance(array, (float, int)):
        array = f'+{float(array)} ' if array >= 0 else f'{float(array)} '
    elif isinstance(array, (pd.Series, pd.DataFrame)):
        array = array.values
    if isinstance(array, np.ndarray):
        if not (array.dtype == object) and array.size:
            signs = pd.Series(array) if array.ndim == 1 else pd.DataFrame(array)
            signs = (signs.pipe(np.sign)
                     .replace([0, 1, -1], ['+', '+', '-']).values)
            array = signs + abs(array).astype(str) + ' '
    return array


def join_exprs(df):
    """
    Helper function to join arrays, series or frames of stings together.

    """
    return ''.join(np.asarray(df).flatten())


# =============================================================================
#  references to vars and cons, rewrite this part to not store every reference
# =============================================================================

def _add_reference(n, df, c, attr, suffix, pnl=True):
    attr_name = attr + suffix
    if pnl:
        if attr_name in n.pnl(c):
            n.pnl(c)[attr_name][df.columns] = df
        else:
            n.pnl(c)[attr_name] = df
        if n.pnl(c)[attr_name].shape[1] == n.df(c).shape[0]:
            n.pnl(c)[attr_name] = n.pnl(c)[attr_name].reindex(columns=n.df(c).index)
    else:
        n.df(c).loc[df.index, attr_name] = df

def set_varref(n, variables, c, attr, pnl=True, spec=''):
    """
    Sets variable references to the network.
    If pnl is False it stores a series of variable names in the static
    dataframe of the given component. The columns name is then given by the
    attribute name attr and the globally define var_ref_suffix.
    If pnl is True if stores the given frame of references in the component
    dict of time-depending quantities, e.g. network.generators_t .
    """
    if not variables.empty:
        if ((c, attr) in n.variables.index) and (spec != ''):
            n.variables.at[idx[c, attr], 'specification'] += ', ' + spec
        else:
            n.variables.loc[idx[c, attr], :] = [pnl, spec]
        _add_reference(n, variables, c, attr, var_ref_suffix, pnl=pnl)

def set_conref(n, constraints, c, attr, pnl=True, spec=''):
    """
    Sets constraint references to the network.
    If pnl is False it stores a series of constraints names in the static
    dataframe of the given component. The columns name is then given by the
    attribute name attr and the globally define con_ref_suffix.
    If pnl is True if stores the given frame of references in the component
    dict of time-depending quantities, e.g. network.generators_t .
    """
    if not constraints.empty:
        if ((c, attr) in n.constraints.index) and (spec != ''):
            n.constraints.at[idx[c, attr], 'specification'] += ', ' + spec
        else:
            n.constraints.loc[idx[c, attr], :] = [pnl, spec]
        _add_reference(n, constraints, c, attr, con_ref_suffix, pnl=pnl)


def get_var(n, c, attr, pop=False):
    '''
    Retrieves variable references for a given static or time-depending
    attribute of a given component. The function looks into n.variables to
    detect whether the variable is a time-dependent or static.

    Parameters
    ----------
    n : pypsa.Network
    c : str
        component name to which the constraint belongs
    attr: str
        attribute name of the constraints

    Example
    -------
    >>> get_var(n, 'Generator', 'p')

    '''
    if n.variables.at[idx[c, attr], 'pnl']:
        if pop:
            return n.pnl(c).pop(attr + var_ref_suffix)
        return n.pnl(c)[attr + var_ref_suffix]
    else:
        if pop:
            return n.df(c).pop(attr + var_ref_suffix)
        return n.df(c)[attr + var_ref_suffix]


def get_con(n, c, attr, pop=False):
    """
    Retrieves constraint references for a given static or time-depending
    attribute of a give component.

    Parameters
    ----------
    n : pypsa.Network
    c : str
        component name to which the constraint belongs
    attr: str
        attribute name of the constraints

    Example
    -------
    get_con(n, 'Generator', 'mu_upper')
    """
    if n.constraints.at[idx[c, attr], 'pnl']:
        if pop:
            return n.pnl(c).pop(attr + con_ref_suffix)
        return n.pnl(c)[attr + con_ref_suffix]
    else:
        if pop:
            return n.df(c).pop(attr + con_ref_suffix)
        return n.df(c)[attr + con_ref_suffix]


def clear_references(n):
    for c in n.iterate_components():
        keys = list(c.pnl.keys())
        for k in keys:
            if (con_ref_suffix in k) or - (var_ref_suffix in k):
                c.pnl.pop(k)
    if 'variables' in n.__dir__():
        del n.variables
    if 'constraints' in n.__dir__():
        del n.constraints


# =============================================================================
# solvers
# =============================================================================

def run_and_read_cbc(n, problem_fn, solution_fn, solver_logfile,
                     solver_options, keep_files, warmstart=None,
                     store_basis=True):
    """
    Solving function. Reads the linear problem file and passes it to the cbc
    solver. If the solution is sucessful it returns variable solutions and
    constraint dual values.

    For more information on the solver options, run 'cbc' in your shell
    """
    #printingOptions is about what goes in solution file
    command = f"cbc -printingOptions all -import {problem_fn} "
    if warmstart:
        command += f'-basisI {warmstart} '
    if (solver_options is not None) and (solver_options != {}):
        command += solver_options
    command += f"-solve -solu {solution_fn} "
    if store_basis:
        n.basis_fn = solution_fn.replace('.sol', '.bas')
        command += f'-basisO {n.basis_fn} '

    if solver_logfile is None:
        os.system(command)
    else:
        result = subprocess.run(command.split(' '), stdout=subprocess.PIPE)
        print(result.stdout.decode('utf-8'), file=open(solver_logfile, 'w'))

    f = open(solution_fn,"r")
    data = f.readline()
    f.close()

    if data.startswith("Optimal - objective value"):
        status = "optimal"
        termination_condition = status
        objective = float(data[len("Optimal - objective value "):])
    elif "Infeasible" in data:
        termination_condition = "infeasible"
    else:
        termination_condition = "other"

    if termination_condition != "optimal":
        return status, termination_condition, None, None, None

    sol = pd.read_csv(solution_fn, header=None, skiprows=[0],
                      sep=r'\s+', usecols=[1,2,3], index_col=0)
    variables_b = sol.index.str[0] == 'x'
    variables_sol = sol[variables_b][2]
    constraints_dual = sol[~variables_b][3]

    if not keep_files:
       os.system("rm "+ problem_fn)
       os.system("rm "+ solution_fn)

    return (status, termination_condition, variables_sol,
            constraints_dual, objective)


def run_and_read_glpk(n, problem_fn, solution_fn, solver_logfile,
                     solver_options, keep_files, warmstart=None,
                     store_basis=True):
    """
    Solving function. Reads the linear problem file and passes it to the glpk
    solver. If the solution is sucessful it returns variable solutions and
    constraint dual values.

    For more information on the glpk solver options:
    https://kam.mff.cuni.cz/~elias/glpk.pdf
    """
    # TODO use --nopresol argument for non-optimal solution output
    command = (f"glpsol --lp {problem_fn} --output {solution_fn}")
    if solver_logfile is not None:
        command += f' --log {solver_logfile}'
    if warmstart:
        command += f' --ini {warmstart}'
    if store_basis:
        n.basis_fn = solution_fn.replace('.sol', '.bas')
        command += f' -w {n.basis_fn}'
    if (solver_options is not None) and (solver_options != {}):
        command += solver_options

    os.system(command)

    data = open(solution_fn)
    info = ''
    linebreak = False
    while not linebreak:
        line = data.readline()
        linebreak = line == '\n'
        info += line
    info = pd.read_csv(io.StringIO(info), sep=':',  index_col=0, header=None)[1]
    status = info.Status.lower().strip()
    objective = float(re.sub('[^0-9]+', '', info.Objective))
    termination_condition = status

    if termination_condition != "optimal":
        return status, termination_condition, None, None, None

    sol = pd.read_fwf(data).set_index('Row name')
    variables_b = sol.index.str[0] == 'x'
    variables_sol = sol[variables_b]['Activity'].astype(float)
    sol = sol[~variables_b]
    constraints_b = sol.index.str[0] == 'c'
    constraints_dual = (pd.to_numeric(sol[constraints_b]['Marginal'], 'coerce')
                        .fillna(0))

    if not keep_files:
       os.system("rm "+ problem_fn)
       os.system("rm "+ solution_fn)

    return (status, termination_condition, variables_sol,
            constraints_dual, objective)


def run_and_read_gurobi(n, problem_fn, solution_fn, solver_logfile,
                        solver_options, keep_files, warmstart=None,
                        store_basis=True):
    """
    Solving function. Reads the linear problem file and passes it to the gurobi
    solver. If the solution is sucessful it returns variable solutions and
    constraint dual values. Gurobipy must be installed for using this function

    For more information on solver options:
    https://www.gurobi.com/documentation/{gurobi_verion}/refman/parameter_descriptions.html
    """
    import gurobipy
    if (solver_logfile is not None) and (solver_options is not None):
        solver_options["logfile"] = solver_logfile

    # disable logging for this part, as gurobi output is doubled otherwise
    logging.disable(50)
    m = gurobipy.read(problem_fn)
    if solver_options is not None:
        for key, value in solver_options.items():
            m.setParam(key, value)
    if warmstart:
        m.read(warmstart)
    m.optimize()
    logging.disable(1)

    if store_basis:
        n.basis_fn = solution_fn.replace('.sol', '.bas')
        try:
            m.write(n.basis_fn)
        except gurobipy.GurobiError:
            logger.info('No model basis stored')
            del n.basis_fn

    if not keep_files:
        os.system("rm "+ problem_fn)

    Status = gurobipy.GRB.Status
    statusmap = {getattr(Status, s) : s.lower() for s in Status.__dir__()
                                                if not s.startswith('_')}
    status = statusmap[m.status]
    termination_condition = status
    if termination_condition != "optimal":
        return status, termination_condition, None, None, None

    variables_sol = pd.Series({v.VarName: v.x for v in m.getVars()})
    constraints_dual = pd.Series({c.ConstrName: c.Pi for c in m.getConstrs()})
    termination_condition = status
    objective = m.ObjVal
    del m
    return (status, termination_condition, variables_sol,
            constraints_dual, objective)

