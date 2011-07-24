#!/usr/bin/python
import pdb

from copy import deepcopy
import math
import StringIO
import threading
import time
import tokenize
from random import random

import pygtk
pygtk.require('2.0')
import gtk, gobject
import numpy as np

import ase.gui.ag
from ase.atoms import Atoms
from ase.gui.view import View
from ase.gui.images import Images
from ase.gui.status import Status
from ase.gui.defaults import read_defaults

import matplotlib
matplotlib.use('GTKAgg')
import matplotlib.pylab as plt


from kmc import units, base, lattice, proclist
import settings


gtk.gdk.threads_init()

class KMC_Model(threading.Thread):
    stopthread = threading.Event()
    def __init__(self, size=10, system_name='kmc_model'):
        super(KMC_Model, self).__init__()

        proclist.init((size,)*int(lattice.model_dimension),system_name, lattice.default_layer, proclist.default_species)
        self.cell_size = np.dot(lattice.unit_cell_size, lattice.system_size)
        self.species_representation = []
        for species in sorted(settings.representations):
            if settings.representations[species].strip():
                self.species_representation.append(eval(settings.representations[species]))
            else:
                self.species_representation.append(Atoms())

        if len(settings.lattice_representation):
            self.lattice_representation = eval(settings.lattice_representation)[0]
        else:
            self.lattice_representation = Atoms()

        self.set_rate_constants()

        # prepare TOF counter
        tofs = []
        for process, tof_count in settings.tof_count.iteritems():
            for tof in tof_count:
                if tof not in tofs:
                    tofs.append(tof)
        self.tofs = tofs

        self.tof_matrix = np.zeros((len(tofs),proclist.nr_of_proc))
        for process, tof_count in settings.tof_count.iteritems():
            process_nr = eval('proclist.%s' % process.lower())
            for tof, tof_factor in tof_count.iteritems():
                self.tof_matrix[tofs.index(tof), process_nr] += tof_factor

        # prepare procstat
        self.procstat = np.zeros((proclist.nr_of_proc,))
        for i in range(proclist.nr_of_proc):
            self.procstat[i] = base.get_procstat(i+1)
        self.time = base.get_kmc_time()

        # history tracking arrays
        self.times = []
        self.tof_hist = []
        self.occupation_hist = []



    def run(self):
        while not self.stopthread.isSet():
            gtk.gdk.threads_enter()
            proclist.do_kmc_step()
            gtk.gdk.threads_leave()

    def stop(self):
        self.stopthread.set()
        base.deallocate_system()

    def run_steps(self, n):
        for i in xrange(n):
            proclist.do_kmc_step()

    def set_rate_constants(self):
        """Tries to evaluate the supplied expression for a rate constant
        to a simple real number and sets it for the corresponding process.
        For the evaluation we draw on predefined natural constants, user defined
        parameters and mathematical functions
        """
        for proc in sorted(settings.rate_constants):
            rate_expr = settings.rate_constants[proc][0]
            if not rate_expr:
                base.set_rate(eval('proclist.%s' % proc.lower()), 0.0)
                continue
            replaced_tokens = []

            # replace useful aliases
            rate_expr = rate_expr.replace('beta','(1/(kboltzmann*T)')

            for i, token, _, _, _ in tokenize.generate_tokens(StringIO.StringIO(rate_expr).readline):
                if token in ['sqrt','exp','sin','cos','pi','pow']:
                    replaced_tokens.append((i,'math.'+token))
                elif ('u_' + token.lower()) in dir(units):
                    replaced_tokens.append((i, str(eval('units.u_' + token.lower()))))

                elif token in settings.parameters:
                    replaced_tokens.append((i, str(settings.parameters[token]['value'])))
                else:
                    replaced_tokens.append((i, token))

            rate_expr = tokenize.untokenize(replaced_tokens)
            try:
                rate_const = eval(rate_expr)
            except Exception as e:
                raise UserWarning("Could not evaluate rate expression: %s\nException: %s" % (rate_expr, e))
            print(proc, rate_const)
            try:
                base.set_rate_const(eval('proclist.%s' % proc.lower()), rate_const)
            except Exception as e:
                raise UserWarning("Could not set %s for process %s!\nException: %s" % (rate_expr, proc, e))

    def get_atoms(self):
        atoms = ase.atoms.Atoms()
        atoms.set_cell(self.cell_size)
        for i in xrange(lattice.system_size[0]):
            for j in xrange(lattice.system_size[1]):
                for k in xrange(lattice.system_size[2]):
                    for n in xrange(1,1+lattice.spuck):
                        species = lattice.get_species([i,j,k,n]) - 1
                        if self.species_representation[species]:
                            atom = deepcopy(self.species_representation[species])
                            atom.translate(np.dot(lattice.unit_cell_size,
                            np.array([i,j,k]) + lattice.site_positions[n-1]))
                            atoms += atom
                    lattice_repr = deepcopy(self.lattice_representation)
                    lattice_repr.translate(np.dot(lattice.unit_cell_size,
                                np.array([i,j,k])))
                    atoms += lattice_repr

        return atoms
class ParamSlider(gtk.HScale):
    def __init__(self, settings, name, value, min, max, set_rate_constants):
        self.settings = settings
        self.param_name = name
        self.value = float(value)
        self.min = float(min)
        self.max = float(max)
        self.set_rate_constants = set_rate_constants
        adjustment = gtk.Adjustment(value=self.value, lower=self.min, upper=self.max)
        gtk.HScale.__init__(self, adjustment)
        self.connect('value-changed',self.value_changed)
        self.set_tooltip_text(self.param_name)

    def value_changed(self, widget):
        self.settings.parameters[self.param_name]['value'] = self.get_value()
        self.set_rate_constants()


class FakeWidget():
    def __init__(self, path):
        self.active = False
        if path.endswith('ShowUnitCell'):
            self.active = True
        elif path.endswith('ShowBonds'):
            self.active = False
        elif path.endswith('ShowAxes'):
            self.active = True

    def get_active(self):
        return self.active

class FakeUI():
    """This is a fudge class to simulate to the View class a non-existing
    menu with included settings
    """
    def __init__(self):
        return self

    def get_widget(self, path):
        widget = FakeWidget(path)
        return widget

class KMC_ViewBox(threading.Thread, View, Images, Status,FakeUI):
    stopthread = threading.Event()
    def __init__(self, vbox, window, rotations='', show_unit_cell=True, show_bonds=False):
        super(KMC_ViewBox, self).__init__()
        self.configured = False
        self.ui = FakeUI.__init__(self)
        self.model = KMC_Model()
        self.model.run_steps(10000)
        self.images = Images()
        self.images.initialize([self.model.get_atoms()])

        self.vbox = vbox
        self.window = window

        self.vbox.connect('scroll-event',self.scroll_event)
        View.__init__(self, self.vbox, rotations)
        Status.__init__(self, self.vbox)
        self.vbox.show()

        self.drawing_area.realize()
        self.scale = 1.0
        self.center = self.model.get_atoms().cell.diagonal()*.5
        self.set_colors()
        self.set_coordinates(0)

        self.model.start()

        # prepare diagrams
        self.data_plot = plt.figure()
        self.tof_diagram = self.data_plot.add_subplot(211)
        self.tof_plots = []
        for tof in self.model.tofs:
            self.tof_plots.append(self.tof_diagram.plot([],[],'b-')[0])
        self.occupation_plots =[]
        self.occupation_diagram = self.data_plot.add_subplot(212)
        for species in range(proclist.nr_of_species):
            self.occupation_plots.append(self.occupation_diagram.plot([], [],)[0])


    def update_vbox(self, atoms):
        self.images = Images()
        self.images.initialize([atoms])
        self.set_coordinates(0)
        self.draw()


    def update_plots(self):
        # Determine turn-over-frequencies
        new_time = base.get_kmc_time()
        new_procstat = np.zeros((proclist.nr_of_proc,))
        for i in range(proclist.nr_of_proc):
            new_procstat[i] = base.get_procstat(i+1)
        tof_data = np.dot(self.model.tof_matrix,  (new_procstat - self.model.procstat))
        # plot TOFs
        while len(self.model.tof_hist) > 100 :
            self.model.tof_hist.pop()
            self.model.times.pop()
        self.model.tof_hist = [tof_data] + self.model.tof_hist
        self.model.times = [ x + base.get_kmc_time() for x in  self.model.times]
        self.model.times = [0.] + self.model.times
        for i, tof_plot in enumerate(self.tof_plots):
            self.tof_plots[i].set_xdata(self.model.times)
            self.tof_plots[i].set_ydata([tof[i] for tof in self.model.tof_hist])

        # plot occupations
        while len(self.model.occupation_hist) > 100 :
            self.model.occupation_hist.pop()
        occupations = proclist.get_occupation().sum(axis=1)/lattice.spuck
        self.model.occupation_hist = [occupations] + self.model.occupation_hist
        #self.model.occupation_hist = [[random() for x in range(proclist.nr_of_species)]] + self.model.occupation_hist
        for i, occupation_plot in enumerate(self.occupation_plots):
            self.occupation_plots[i].set_xdata(self.model.times)
            self.occupation_plots[i].set_ydata([occ[i] for occ in self.model.occupation_hist])

        plt.ioff()
        self.data_plot.canvas.draw_idle()
        plt.axes([0, 100, 0, 1])
        plt.show()

        self.model.procstat[:] = new_procstat
        self.model.time = new_time

        return False

    def run(self):
        while not self.stopthread.isSet():
            atoms = self.model.get_atoms()
            gobject.idle_add(self.update_vbox, atoms)
            gobject.idle_add(self.update_plots)
            time.sleep(0.05)

    def stop(self):
        self.model.stop()
        self.stopthread.set()


    def scroll_event(self, window, event):
        """Zoom in/out when using mouse wheel"""
        x = 1.0
        if event.direction == gtk.gdk.SCROLL_UP:
            x = 1.2
        elif event.direction == gtk.gdk.SCROLL_DOWN:
            x = 1.0 / 1.2
        self._do_zoom(x)

    def _do_zoom(self, x):
        """Utility method for zooming"""
        self.scale *= x

class KMC_Viewer():
    def __init__(self):
        self.window = gtk.Window(gtk.WINDOW_TOPLEVEL)
        self.window.set_position(gtk.WIN_POS_CENTER)
        self.window.connect('delete-event',self.exit)

        self.vbox = gtk.VBox()
        self.window.add(self.vbox)
        self.kmc_viewbox = KMC_ViewBox(self.vbox, self.window)
        for param_name in filter(lambda p: settings.parameters[p]['adjustable'], settings.parameters):
            param = settings.parameters[param_name]
            slider = ParamSlider(settings, param_name, param['value'], param['min'], param['max'], self.kmc_viewbox.model.set_rate_constants)
            self.vbox.add(slider)
            self.vbox.set_child_packing(slider, expand=False, fill=False, padding=0, pack_type=gtk.PACK_START)
        self.window.show_all()
        self.kmc_viewbox.start()


    def exit(self,widget,event):
        self.kmc_viewbox.stop()
        gtk.main_quit()
        return True


if __name__ == '__main__':
    gobject.threads_init()
    viewer = KMC_Viewer()
    gtk.main()
