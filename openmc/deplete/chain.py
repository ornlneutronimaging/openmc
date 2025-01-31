"""chain module.

This module contains information about a depletion chain.  A depletion chain is
loaded from an .xml file and all the nuclides are linked together.
"""

from io import StringIO
from itertools import chain
import math
import re
from collections import OrderedDict, defaultdict
from collections.abc import Mapping
from numbers import Real
from warnings import warn

from openmc.checkvalue import check_type, check_greater_than
from openmc.data import gnd_name, zam

# Try to use lxml if it is available. It preserves the order of attributes and
# provides a pretty-printer by default. If not available,
# use OpenMC function to pretty print.
try:
    import lxml.etree as ET
    _have_lxml = True
except ImportError:
    import xml.etree.ElementTree as ET
    _have_lxml = False
import scipy.sparse as sp

import openmc.data
from openmc._xml import clean_indentation
from .nuclide import Nuclide, DecayTuple, ReactionTuple


# tuple of (reaction name, possible MT values, (dA, dZ)) where dA is the change
# in the mass number and dZ is the change in the atomic number
_REACTIONS = [
    ('(n,2n)', set(chain([16], range(875, 892))), (-1, 0)),
    ('(n,3n)', {17}, (-2, 0)),
    ('(n,4n)', {37}, (-3, 0)),
    ('(n,gamma)', {102}, (1, 0)),
    ('(n,p)', set(chain([103], range(600, 650))), (0, -1)),
    ('(n,a)', set(chain([107], range(800, 850))), (-3, -2))
]


def replace_missing(product, decay_data):
    """Replace missing product with suitable decay daughter.

    Parameters
    ----------
    product : str
        Name of product in GND format, e.g. 'Y86_m1'.
    decay_data : dict
        Dictionary of decay data

    Returns
    -------
    product : str
        Replacement for missing product in GND format.

    """
    # Determine atomic number, mass number, and metastable state
    Z, A, state = openmc.data.zam(product)
    symbol = openmc.data.ATOMIC_SYMBOL[Z]

    # Replace neutron with proton
    if Z == 0 and A == 1:
        return 'H1'

    # First check if ground state is available
    if state:
        product = '{}{}'.format(symbol, A)

    # Find isotope with longest half-life
    half_life = 0.0
    for nuclide, data in decay_data.items():
        m = re.match(r'{}(\d+)(?:_m\d+)?'.format(symbol), nuclide)
        if m:
            # If we find a stable nuclide, stop search
            if data.nuclide['stable']:
                mass_longest_lived = int(m.group(1))
                break
            if data.half_life.nominal_value > half_life:
                mass_longest_lived = int(m.group(1))
                half_life = data.half_life.nominal_value

    # If mass number of longest-lived isotope is less than that of missing
    # product, assume it undergoes beta-. Otherwise assume beta+.
    beta_minus = (mass_longest_lived < A)

    # Iterate until we find an existing nuclide
    while product not in decay_data:
        if Z > 98:
            Z -= 2
            A -= 4
        else:
            if beta_minus:
                Z += 1
            else:
                Z -= 1
        product = '{}{}'.format(openmc.data.ATOMIC_SYMBOL[Z], A)

    return product


_SECONDARY_PARTICLES = {
    "(n,p)": ["H1"], "(n,d)": ["H2"], "(n,t)": ["H3"], "(n,3He)": ["He3"],
    "(n,a)": ["He4"], "(n,2nd)": ["H2"], "(n,na)": ["He4"], "(n,3na)": ["He4"],
    "(n,n3a)": ["He4"] * 3, "(n,2na)": ["He4"], "(n,np)": ["H1"],
    "(n,n2a)": ["He4"] * 2, "(n,2n2a)": ["He4"] * 2, "(n,nd)": ["H2"],
    "(n,nt)": ["H3"], "(n,nHe-3)": ["He3"], "(n,nd2a)": ["H2", "He4"],
    "(n,nt2a)": ["H3", "He4", "He4"], "(n,2np)": ["H1"], "(n,3np)": ["H1"],
    "(n,n2p)": ["H1"] * 2, "(n,2a)": ["He4"] * 2, "(n,3a)": ["He4"] * 3,
    "(n,2p)": ["H1"] * 2, "(n,pa)": ["H1", "He4"],
    "(n,t2a)": ["H3", "He4", "He4"], "(n,d2a)": ["H2", "He4", "He4"],
    "(n,pd)": ["H1", "H2"], "(n,pt)": ["H1", "H3"], "(n,da)": ["H2", "He4"]}


class Chain(object):
    """Full representation of a depletion chain.

    A depletion chain can be created by using the :meth:`from_endf` method which
    requires a list of ENDF incident neutron, decay, and neutron fission product
    yield sublibrary files. The depletion chain used during a depletion
    simulation is indicated by either an argument to
    :class:`openmc.deplete.Operator` or through the
    ``depletion_chain`` item in the :envvar:`OPENMC_CROSS_SECTIONS`
    environment variable.

    Attributes
    ----------
    nuclides : list of openmc.deplete.Nuclide
        Nuclides present in the chain.
    reactions : list of str
        Reactions that are tracked in the depletion chain
    nuclide_dict : OrderedDict of str to int
        Maps a nuclide name to an index in nuclides.
    """

    def __init__(self):
        self.nuclides = []
        self.reactions = []
        self.nuclide_dict = OrderedDict()

    def __contains__(self, nuclide):
        return nuclide in self.nuclide_dict

    def __getitem__(self, name):
        """Get a Nuclide by name."""
        return self.nuclides[self.nuclide_dict[name]]

    def __len__(self):
        """Number of nuclides in chain."""
        return len(self.nuclides)

    @classmethod
    def from_endf(cls, decay_files, fpy_files, neutron_files):
        """Create a depletion chain from ENDF files.

        Parameters
        ----------
        decay_files : list of str
            List of ENDF decay sub-library files
        fpy_files : list of str
            List of ENDF neutron-induced fission product yield sub-library files
        neutron_files : list of str
            List of ENDF neutron reaction sub-library files

        """
        chain = cls()

        # Create dictionary mapping target to filename
        print('Processing neutron sub-library files...')
        reactions = {}
        for f in neutron_files:
            evaluation = openmc.data.endf.Evaluation(f)
            name = evaluation.gnd_name
            reactions[name] = {}
            for mf, mt, nc, mod in evaluation.reaction_list:
                if mf == 3:
                    file_obj = StringIO(evaluation.section[3, mt])
                    openmc.data.endf.get_head_record(file_obj)
                    q_value = openmc.data.endf.get_cont_record(file_obj)[1]
                    reactions[name][mt] = q_value

        # Determine what decay and FPY nuclides are available
        print('Processing decay sub-library files...')
        decay_data = {}
        for f in decay_files:
            data = openmc.data.Decay(f)
            # Skip decay data for neutron itself
            if data.nuclide['atomic_number'] == 0:
                continue
            decay_data[data.nuclide['name']] = data

        print('Processing fission product yield sub-library files...')
        fpy_data = {}
        for f in fpy_files:
            data = openmc.data.FissionProductYields(f)
            fpy_data[data.nuclide['name']] = data

        print('Creating depletion_chain...')
        missing_daughter = []
        missing_rx_product = []
        missing_fpy = []
        missing_fp = []

        for idx, parent in enumerate(sorted(decay_data, key=openmc.data.zam)):
            data = decay_data[parent]

            nuclide = Nuclide()
            nuclide.name = parent

            chain.nuclides.append(nuclide)
            chain.nuclide_dict[parent] = idx

            if not data.nuclide['stable'] and data.half_life.nominal_value != 0.0:
                nuclide.half_life = data.half_life.nominal_value
                nuclide.decay_energy = sum(E.nominal_value for E in
                                           data.average_energies.values())
                sum_br = 0.0
                for i, mode in enumerate(data.modes):
                    type_ = ','.join(mode.modes)
                    if mode.daughter in decay_data:
                        target = mode.daughter
                    else:
                        print('missing {} {} {}'.format(parent, ','.join(mode.modes), mode.daughter))
                        target = replace_missing(mode.daughter, decay_data)

                    # Write branching ratio, taking care to ensure sum is unity
                    br = mode.branching_ratio.nominal_value
                    sum_br += br
                    if i == len(data.modes) - 1 and sum_br != 1.0:
                        br = 1.0 - sum(m.branching_ratio.nominal_value
                                       for m in data.modes[:-1])

                    # Append decay mode
                    nuclide.decay_modes.append(DecayTuple(type_, target, br))

            if parent in reactions:
                reactions_available = set(reactions[parent].keys())
                for name, mts, changes in _REACTIONS:
                    if mts & reactions_available:
                        delta_A, delta_Z = changes
                        A = data.nuclide['mass_number'] + delta_A
                        Z = data.nuclide['atomic_number'] + delta_Z
                        daughter = '{}{}'.format(openmc.data.ATOMIC_SYMBOL[Z], A)

                        if name not in chain.reactions:
                            chain.reactions.append(name)

                        if daughter not in decay_data:
                            missing_rx_product.append((parent, name, daughter))

                        # Store Q value
                        for mt in sorted(mts):
                            if mt in reactions[parent]:
                                q_value = reactions[parent][mt]
                                break
                        else:
                            q_value = 0.0

                        nuclide.reactions.append(ReactionTuple(
                            name, daughter, q_value, 1.0))

                if any(mt in reactions_available for mt in [18, 19, 20, 21, 38]):
                    if parent in fpy_data:
                        q_value = reactions[parent][18]
                        nuclide.reactions.append(
                            ReactionTuple('fission', 0, q_value, 1.0))

                        if 'fission' not in chain.reactions:
                            chain.reactions.append('fission')
                    else:
                        missing_fpy.append(parent)

            if parent in fpy_data:
                fpy = fpy_data[parent]

                if fpy.energies is not None:
                    nuclide.yield_energies = fpy.energies
                else:
                    nuclide.yield_energies = [0.0]

                for E, table in zip(nuclide.yield_energies, fpy.independent):
                    yield_replace = 0.0
                    yields = defaultdict(float)
                    for product, y in table.items():
                        # Handle fission products that have no decay data available
                        if product not in decay_data:
                            daughter = replace_missing(product, decay_data)
                            product = daughter
                            yield_replace += y.nominal_value

                        yields[product] += y.nominal_value

                    if yield_replace > 0.0:
                        missing_fp.append((parent, E, yield_replace))

                    nuclide.yield_data[E] = []
                    for k in sorted(yields, key=openmc.data.zam):
                        nuclide.yield_data[E].append((k, yields[k]))

        # Display warnings
        if missing_daughter:
            print('The following decay modes have daughters with no decay data:')
            for mode in missing_daughter:
                print('  {}'.format(mode))
            print('')

        if missing_rx_product:
            print('The following reaction products have no decay data:')
            for vals in missing_rx_product:
                print('{} {} -> {}'.format(*vals))
            print('')

        if missing_fpy:
            print('The following fissionable nuclides have no fission product yields:')
            for parent in missing_fpy:
                print('  ' + parent)
            print('')

        if missing_fp:
            print('The following nuclides have fission products with no decay data:')
            for vals in missing_fp:
                print('  {}, E={} eV (total yield={})'.format(*vals))

        return chain

    @classmethod
    def from_xml(cls, filename, fission_q=None):
        """Reads a depletion chain XML file.

        Parameters
        ----------
        filename : str
            The path to the depletion chain XML file.
        fission_q : dict, optional
            Dictionary of nuclides and their fission Q values [eV].
            If not given, values will be pulled from ``filename``

        """
        chain = cls()

        if fission_q is not None:
            check_type("fission_q", fission_q, Mapping)
        else:
            fission_q = {}

        # Load XML tree
        root = ET.parse(str(filename))

        for i, nuclide_elem in enumerate(root.findall('nuclide')):
            this_q = fission_q.get(nuclide_elem.get("name"))

            nuc = Nuclide.from_xml(nuclide_elem, this_q)
            chain.nuclide_dict[nuc.name] = i

            # Check for reaction paths
            for rx in nuc.reactions:
                if rx.type not in chain.reactions:
                    chain.reactions.append(rx.type)

            chain.nuclides.append(nuc)

        return chain

    def export_to_xml(self, filename):
        """Writes a depletion chain XML file.

        Parameters
        ----------
        filename : str
            The path to the depletion chain XML file.

        """

        root_elem = ET.Element('depletion_chain')
        for nuclide in self.nuclides:
            root_elem.append(nuclide.to_xml_element())

        tree = ET.ElementTree(root_elem)
        if _have_lxml:
            tree.write(str(filename), encoding='utf-8', pretty_print=True)
        else:
            clean_indentation(root_elem)
            tree.write(str(filename), encoding='utf-8')

    def form_matrix(self, rates):
        """Forms depletion matrix.

        Parameters
        ----------
        rates : numpy.ndarray
            2D array indexed by (nuclide, reaction)

        Returns
        -------
        scipy.sparse.csr_matrix
            Sparse matrix representing depletion.

        """
        matrix = defaultdict(float)
        reactions = set()

        for i, nuc in enumerate(self.nuclides):

            if nuc.n_decay_modes != 0:
                # Decay paths
                # Loss
                decay_constant = math.log(2) / nuc.half_life

                if decay_constant != 0.0:
                    matrix[i, i] -= decay_constant

                # Gain
                for _, target, branching_ratio in nuc.decay_modes:
                    # Allow for total annihilation for debug purposes
                    if target != 'Nothing':
                        branch_val = branching_ratio * decay_constant

                        if branch_val != 0.0:
                            k = self.nuclide_dict[target]
                            matrix[k, i] += branch_val

            if nuc.name in rates.index_nuc:
                # Extract all reactions for this nuclide in this cell
                nuc_ind = rates.index_nuc[nuc.name]
                nuc_rates = rates[nuc_ind, :]

                for r_type, target, _, br in nuc.reactions:
                    # Extract reaction index, and then final reaction rate
                    r_id = rates.index_rx[r_type]
                    path_rate = nuc_rates[r_id]

                    # Loss term -- make sure we only count loss once for
                    # reactions with branching ratios
                    if r_type not in reactions:
                        reactions.add(r_type)
                        if path_rate != 0.0:
                            matrix[i, i] -= path_rate

                    # Gain term; allow for total annihilation for debug purposes
                    if target != 'Nothing':
                        if r_type != 'fission':
                            if path_rate != 0.0:
                                k = self.nuclide_dict[target]
                                matrix[k, i] += path_rate * br
                        else:
                            # Assume that we should always use thermal fission
                            # yields. At some point it would be nice to account
                            # for the energy-dependence..
                            energy, data = sorted(nuc.yield_data.items())[0]
                            for product, y in data:
                                yield_val = y * path_rate
                                if yield_val != 0.0:
                                    k = self.nuclide_dict[product]
                                    matrix[k, i] += yield_val

                # Clear set of reactions
                reactions.clear()

        # Use DOK matrix as intermediate representation, then convert to CSR and return
        n = len(self)
        matrix_dok = sp.dok_matrix((n, n))
        dict.update(matrix_dok, matrix)
        return matrix_dok.tocsr()

    def get_branch_ratios(self, reaction="(n,gamma)"):
        """Return a dictionary with reaction branching ratios

        Parameters
        ----------
        reaction : str, optional
            Reaction name like ``"(n,gamma)"`` [default], or
            ``"(n,alpha)"``.

        Returns
        -------
        branches : dict
            nested dict of parent nuclide keys with reaction targets and
            branching ratios. Consider the capture, ``"(n,gamma)"``,
            reaction for Am241::

                {"Am241": {"Am242": 0.91, "Am242_m1": 0.09}}

        See Also
        --------
        :meth:`set_branch_ratios`
        """

        capt = {}
        for nuclide in self.nuclides:
            nuc_capt = {}
            for rx in nuclide.reactions:
                if rx.type == reaction and rx.branching_ratio != 1.0:
                    nuc_capt[rx.target] = rx.branching_ratio
            if len(nuc_capt) > 0:
                capt[nuclide.name] = nuc_capt
        return capt

    def set_branch_ratios(self, branch_ratios, reaction="(n,gamma)",
                          strict=True, tolerance=1e-5):
        """Set the branching ratios for a given reactions

        Parameters
        ----------
        branch_ratios : dict of {str: {str: float}}
            Capture branching ratios to be inserted.
            First layer keys are names of parent nuclides, e.g.
            ``"Am241"``. The branching ratios for these
            parents will be modified. Corresponding values are
            dictionaries of ``{target: branching_ratio}``
        reaction : str, optional
            Reaction name like ``"(n,gamma)"`` [default], or
            ``"(n, alpha)"``.
        strict : bool, optional
            Error control. If this evalutes to ``True``, then errors will
            be raised if inconsistencies are found. Otherwise, warnings
            will be raised for most issues.
        tolerance : float, optional
            Tolerance on the sum of all branching ratios for a
            single parent. Will be checked with::

                1 - tol < sum_br < 1 + tol

        Raises
        ------
        IndexError
            If no isotopes were found on the chain that have the requested
            reaction
        KeyError
            If ``strict`` evaluates to ``False`` and a parent isotope in
            ``branch_ratios`` does not exist on the chain
        AttributeError
            If ``strict`` evaluates to ``False`` and a parent isotope in
            ``branch_ratios`` does not have the requested reaction
        ValueError
            If ``strict`` evalutes to ``False`` and the sum of one parents
            branch ratios is outside  1 +/- ``tolerance``

        See Also
        --------
        :meth:`get_branch_ratios`
        """

        # Store some useful information through the validation stage

        sums = {}
        rxn_ix_map = {}
        grounds = {}

        tolerance = abs(tolerance)

        missing_parents = set()
        missing_products = {}
        missing_reaction = set()
        bad_sums = {}

        # Secondary products, like alpha particles, should not be modified
        secondary = _SECONDARY_PARTICLES.get(reaction, [])

        # Check for validity before manipulation

        for parent, sub in branch_ratios.items():
            if parent not in self:
                if strict:
                    raise KeyError(parent)
                missing_parents.add(parent)
                continue

            # Make sure all products are present in the chain

            prod_flag = False

            for product in sub:
                if product not in self:
                    if strict:
                        raise KeyError(product)
                    missing_products[parent] = product
                    prod_flag = True
                    break

            if prod_flag:
                continue

            # Make sure this nuclide has the reaction

            indexes = []
            for ix, rx in enumerate(self[parent].reactions):
                if rx.type == reaction and rx.target not in secondary:
                    indexes.append(ix)
                    if "_m" not in rx.target:
                        grounds[parent] = rx.target

            if len(indexes) == 0:
                if strict:
                    raise AttributeError(
                        "Nuclide {} does not have {} reactions".format(
                            parent, reaction))
                missing_reaction.add(parent)
                continue

            this_sum = sum(sub.values())
            # sum of branching ratios can be lower than 1 if no ground
            # target is given, but never greater
            if (this_sum >= 1 + tolerance or (grounds[parent] in sub
                                              and this_sum <= 1 - tolerance)):
                if strict:
                    msg = ("Sum of {} branching ratios for {} "
                           "({:7.3f}) outside tolerance of 1 +/- "
                           "{:5.3e}".format(
                               reaction, parent, this_sum, tolerance))
                    raise ValueError(msg)
                bad_sums[parent] = this_sum
            else:
                rxn_ix_map[parent] = indexes
                sums[parent] = this_sum

        if len(rxn_ix_map) == 0:
            raise IndexError(
                "No {} reactions found in this {}".format(
                    reaction, self.__class__.__name__))

        if len(missing_parents) > 0:
            warn("The following nuclides were not found in {}: {}".format(
                 self.__class__.__name__, ", ".join(sorted(missing_parents))))

        if len(missing_reaction) > 0:
            warn("The following nuclides did not have {} reactions: "
                 "{}".format(reaction, ", ".join(sorted(missing_reaction))))

        if len(missing_products) > 0:
            tail = ("{} -> {}".format(k, v)
                    for k, v in sorted(missing_products.items()))
            warn("The following products were not found in the {} and "
                 "parents were unmodified: \n{}".format(
                     self.__class__.__name__, ", ".join(tail)))

        if len(bad_sums) > 0:
            tail = ("{}: {:5.3f}".format(k, s)
                    for k, s in sorted(bad_sums.items()))
            warn("The following parent nuclides were given {} branch ratios "
                 "with a sum outside tolerance of 1 +/- {:5.3e}:\n{}".format(
                     reaction, tolerance, "\n".join(tail)))

        # Insert new ReactionTuples with updated branch ratios

        for parent_name, rxn_index in rxn_ix_map.items():

            parent = self[parent_name]
            new_ratios = branch_ratios[parent_name]
            rxn_index = rxn_ix_map[parent_name]

            # Assume Q value is independent of target state
            rxn_Q = parent.reactions[rxn_index[0]].Q

            # Remove existing reactions

            for ix in reversed(rxn_index):
                parent.reactions.pop(ix)

            all_meta = True

            for tgt, br in new_ratios.items():
                all_meta = all_meta and ("_m" in tgt)
                parent.reactions.append(ReactionTuple(
                    reaction, tgt, rxn_Q, br))

            if all_meta and sums[parent_name] != 1.0:
                ground_br = 1.0 - sums[parent_name]
                ground_tgt = grounds.get(parent_name)
                if ground_tgt is None:
                    pz, pa, pm = zam(parent_name)
                    ground_tgt = gnd_name(pz, pa + 1, 0)
                new_ratios[ground_tgt] = ground_br
                parent.reactions.append(ReactionTuple(
                    reaction, ground_tgt, rxn_Q, ground_br))

    def validate(self, strict=True, quiet=False, tolerance=1e-4):
        """Search for possible inconsistencies

        The following checks are performed for all nuclides present:

            1) For all non-fission reactions, does the sum of branching
               ratios equal about one?
            2) For fission reactions, does the sum of fission yield
               fractions equal about two?

        Parameters
        ----------
        strict : bool, optional
            Raise exceptions at the first inconsistency if true.
            Otherwise mark a warning
        quiet : bool, optional
            Flag to suppress warnings and return immediately at
            the first inconsistency. Used only if
            ``strict`` does not evaluate to ``True``.
        tolerance : float, optional
            Absolute tolerance for comparisons. Used to compare computed
            value ``x`` to intended value ``y`` as::

                valid = (y - tolerance <= x <= y + tolerance)

        Returns
        -------
        valid : bool
            True if no inconsistencies were found

        Raises
        ------
        ValueError
            If ``strict`` evaluates to ``True`` and an inconistency was
            found

        See Also
        --------
        openmc.deplete.Nuclide.validate
        """
        check_type("tolerance", tolerance, Real)
        check_greater_than("tolerance", tolerance, 0.0, True)
        valid = True
        # Sort through nuclides by name
        for name in sorted(self.nuclide_dict):
            stat = self[name].validate(strict, quiet, tolerance)
            if quiet and not stat:
                return stat
            valid = valid and stat
        return valid
