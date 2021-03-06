# -*- coding: utf-8 -*-
from djangocms_text_ckeditor.models import Text
from django.contrib.admin.sites import site
from django.contrib.admin.utils import unquote
from django.contrib.auth import get_user_model
from django.contrib.auth.models import AnonymousUser, Group, Permission
from django.contrib.sites.models import Site
from django.core.management import call_command
from django.core.urlresolvers import reverse
from django.db.models import Q
from django.test.client import RequestFactory
from django.test.utils import override_settings
from django.utils.http import urlencode

from cms.api import (add_plugin, assign_user_to_page, create_page,
                     create_page_user, publish_page)
from cms.admin.forms import save_permissions
from cms.cms_menus import get_visible_pages
from cms.constants import PUBLISHER_STATE_PENDING
from cms.management.commands.subcommands.moderator import log
from cms.models import Page, CMSPlugin, Title, ACCESS_PAGE
from cms.models.permissionmodels import (ACCESS_DESCENDANTS,
                                         ACCESS_PAGE_AND_DESCENDANTS,
                                         PagePermission,
                                         GlobalPagePermission)
from cms.plugin_pool import plugin_pool
from cms.test_utils.testcases import (URL_CMS_PAGE_ADD, URL_CMS_PLUGIN_REMOVE,
                                      URL_CMS_PLUGIN_ADD, CMSTestCase)
from cms.test_utils.util.context_managers import disable_logger
from cms.test_utils.util.fuzzy_int import FuzzyInt
from cms.utils.i18n import force_language
from cms.utils.page_resolver import get_page_from_path
from cms.utils.permissions import (has_page_add_permission_from_request,
                                   has_page_change_permission,
                                   has_generic_permission)


def fake_tree_attrs(page):
    page.depth = 1
    page.path = '0001'
    page.numchild = 0


@override_settings(CMS_PERMISSION=True)
class PermissionModeratorTests(CMSTestCase):
    """Permissions and moderator together

    Fixtures contains 3 users and 1 published page and some other stuff

    Users:
        1. `super`: superuser
        2. `master`: user with permissions to all applications
        3. `slave`: user assigned to page `slave-home`

    Pages:
        1. `home`:
            - published page
            - master can do anything on its subpages, but not on home!

        2. `master`:
            - published page
            - created by super
            - `master` can do anything on it and its descendants
            - subpages:

        3.       `slave-home`:
                    - not published
                    - assigned slave user which can add/change/delete/
                      move/publish this page and its descendants
                    - `master` user want to moderate this page and all descendants

        4. `pageA`:
            - created by super
            - master can add/change/delete on it and descendants
    """
    #TODO: Split this test case into one that tests publish functionality, and
    #TODO: one that tests permission inheritance. This is too complex.

    def setUp(self):
        # create super user
        self.user_super = self._create_user("super", is_staff=True,
                                            is_superuser=True)
        self.user_staff = self._create_user("staff", is_staff=True,
                                            add_default_permissions=True)
        self.user_master = self._create_user("master", is_staff=True,
                                             add_default_permissions=True)
        self.user_slave = self._create_user("slave", is_staff=True,
                                            add_default_permissions=True)
        self.user_normal = self._create_user("normal", is_staff=False)
        self.user_normal.user_permissions.add(
            Permission.objects.get(codename='publish_page'))

        with self.login_user_context(self.user_super):
            self.home_page = create_page("home", "nav_playground.html", "en",
                                         created_by=self.user_super)

            # master page & master user

            self.master_page = create_page("master", "nav_playground.html", "en")

            # create non global, non staff user
            self.user_non_global = self._create_user("nonglobal")

            # assign master user under home page
            assign_user_to_page(self.home_page, self.user_master,
                                grant_on=ACCESS_DESCENDANTS, grant_all=True)

            # and to master page
            assign_user_to_page(self.master_page, self.user_master,
                                grant_on=ACCESS_PAGE_AND_DESCENDANTS, grant_all=True)

            # slave page & slave user

            self.slave_page = create_page("slave-home", "col_two.html", "en",
                                          parent=self.master_page, created_by=self.user_super)

            assign_user_to_page(self.slave_page, self.user_slave, grant_all=True)

            # create page_b
            page_b = create_page("pageB", "nav_playground.html", "en", created_by=self.user_super)
            # Normal user

            # it's allowed for the normal user to view the page
            assign_user_to_page(page_b, self.user_normal, can_view=True)

            # create page_a - sample page from master

            page_a = create_page("pageA", "nav_playground.html", "en",
                                 created_by=self.user_super)
            assign_user_to_page(page_a, self.user_master,
                                can_add=True, can_change=True, can_delete=True, can_publish=True,
                                can_move_page=True)

            # publish after creating all drafts
            publish_page(self.home_page, self.user_super, 'en')

            publish_page(self.master_page, self.user_super, 'en')

            self.page_b = publish_page(page_b, self.user_super, 'en')

    def _add_plugin(self, user, page):
        """
        Add a plugin using the test client to check for permissions.
        """
        with self.login_user_context(user):
            placeholder = page.placeholders.all()[0]
            post_data = {
                'body': 'Test'
            }
            url = URL_CMS_PLUGIN_ADD + '?' + urlencode({
                'plugin_language': 'en',
                'placeholder_id': placeholder.pk,
                'plugin_type': 'TextPlugin'
            })
            response = self.client.post(url, post_data)
            self.assertEqual(response.status_code, 302)
            return response.content.decode('utf8')

    def test_super_can_add_page_to_root(self):
        with self.login_user_context(self.user_super):
            response = self.client.get(URL_CMS_PAGE_ADD)
            self.assertEqual(response.status_code, 200)

    def test_master_cannot_add_page_to_root(self):
        with self.login_user_context(self.user_master):
            response = self.client.get(URL_CMS_PAGE_ADD)
            self.assertEqual(response.status_code, 403)

    def test_slave_cannot_add_page_to_root(self):
        with self.login_user_context(self.user_slave):
            response = self.client.get(URL_CMS_PAGE_ADD)
            self.assertEqual(response.status_code, 403)

    def test_slave_can_add_page_under_slave_home(self):
        with self.login_user_context(self.user_slave):
            # move to admin.py?
            # url = URL_CMS_PAGE_ADD + "?target=%d&position=last-child" % slave_page.pk

            # can he even access it over get?
            # response = self.client.get(url)
            # self.assertEqual(response.status_code, 200)

            # add page
            page = create_page("page", "nav_playground.html", "en",
                               parent=self.slave_page, created_by=self.user_slave)
            # adds user_slave as page moderator for this page
            # public model shouldn't be available yet, because of the moderation
            # moderators and approval ok?

            # must not have public object yet
            self.assertFalse(page.publisher_public)

            self.assertObjectExist(Title.objects, slug="page")
            self.assertObjectDoesNotExist(Title.objects.public(), slug="page")

            self.assertTrue(has_generic_permission(page.pk, self.user_slave, "publish", 1))

            # publish as slave, published as user_master before
            publish_page(page, self.user_slave, 'en')
            # user_slave is moderator for this page
            # approve / publish as user_slave
            # user master should be able to approve as well

    @override_settings(
        CMS_PLACEHOLDER_CONF={
            'col_left': {
                'default_plugins': [
                    {
                        'plugin_type': 'TextPlugin',
                        'values': {
                            'body': 'Lorem ipsum dolor sit amet, consectetur adipisicing elit. Culpa, repellendus, delectus, quo quasi ullam inventore quod quam aut voluptatum aliquam voluptatibus harum officiis officia nihil minus unde accusamus dolorem repudiandae.'
                        },
                    },
                ]
            },
        },
    )
    def test_default_plugins(self):
        with self.login_user_context(self.user_slave):
            self.assertEqual(CMSPlugin.objects.count(), 0)
            response = self.client.get(self.slave_page.get_absolute_url(), {'edit': 1})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(CMSPlugin.objects.count(), 1)

    def test_page_added_by_slave_can_be_published_by_user_master(self):
        # add page
        page = create_page("page", "nav_playground.html", "en",
                           parent=self.slave_page, created_by=self.user_slave)
        # same as test_slave_can_add_page_under_slave_home

        # must not have public object yet
        self.assertFalse(page.publisher_public)

        self.assertTrue(has_generic_permission(page.pk, self.user_master, "publish", page.site.pk))
        # should be True user_master should have publish permissions for children as well
        publish_page(self.slave_page, self.user_master, 'en')
        page = publish_page(page, self.user_master, 'en')
        self.assertTrue(page.publisher_public_id)
        # user_master is moderator for top level page / but can't approve descendants?
        # approve / publish as user_master
        # user master should be able to approve descendants

    def test_super_can_add_plugin(self):
        self._add_plugin(self.user_super, page=self.slave_page)

    def test_master_can_add_plugin(self):
        self._add_plugin(self.user_master, page=self.slave_page)

    def test_slave_can_add_plugin(self):
        self._add_plugin(self.user_slave, page=self.slave_page)

    def test_same_order(self):
        # create 4 pages
        slugs = []
        for i in range(0, 4):
            page = create_page("page", "nav_playground.html", "en",
                               parent=self.home_page)
            slug = page.title_set.drafts()[0].slug
            slugs.append(slug)

        # approve last 2 pages in reverse order
        for slug in reversed(slugs[2:]):
            page = self.assertObjectExist(Page.objects.drafts(), title_set__slug=slug)
            page = publish_page(page, self.user_master, 'en')
            self.check_published_page_attributes(page)

    def test_create_copy_publish(self):
        # create new page to copy
        page = create_page("page", "nav_playground.html", "en",
                           parent=self.slave_page)

        # copy it under home page...
        # TODO: Use page.copy_page here
        with self.login_user_context(self.user_master):
            copied_page = self.copy_page(page, self.home_page)

        page = publish_page(copied_page, self.user_master, 'en')
        self.check_published_page_attributes(page)

    def test_create_publish_copy(self):
        # create new page to copy
        page = create_page("page", "nav_playground.html", "en",
                           parent=self.home_page)

        page = publish_page(page, self.user_master, 'en')

        # copy it under master page...
        # TODO: Use page.copy_page here
        with self.login_user_context(self.user_master):
            copied_page = self.copy_page(page, self.master_page)

        self.check_published_page_attributes(page)
        copied_page = publish_page(copied_page, self.user_master, 'en')
        self.check_published_page_attributes(copied_page)

    def test_subtree_needs_approval(self):
        # create page under slave_page
        page = create_page("parent", "nav_playground.html", "en",
                           parent=self.home_page)
        self.assertFalse(page.publisher_public)

        # create subpage under page
        subpage = create_page("subpage", "nav_playground.html", "en", parent=page)
        self.assertFalse(subpage.publisher_public)

        # publish both of them in reverse order
        subpage = publish_page(subpage, self.user_master, 'en')

        # subpage should not be published, because parent is not published
        # yet, should be marked as `publish when parent`
        self.assertFalse(subpage.publisher_public)

        # publish page (parent of subage), so subpage must be published also
        page = publish_page(page, self.user_master, 'en')
        self.assertNotEqual(page.publisher_public, None)

        # reload subpage, it was probably changed
        subpage = self.reload(subpage)

        # parent was published, so subpage must be also published..
        self.assertNotEqual(subpage.publisher_public, None)

        #check attributes
        self.check_published_page_attributes(page)
        self.check_published_page_attributes(subpage)

    def test_subtree_with_super(self):
        # create page under root
        page = create_page("page", "nav_playground.html", "en")
        self.assertFalse(page.publisher_public)

        # create subpage under page
        subpage = create_page("subpage", "nav_playground.html", "en",
                              parent=page)
        self.assertFalse(subpage.publisher_public)

        # tree id must be the same
        self.assertEqual(page.path[0:4], subpage.path[0:4])

        # publish both of them
        page = self.reload(page)
        page = publish_page(page, self.user_super, 'en')
        # reload subpage, there were an path change
        subpage = self.reload(subpage)
        self.assertEqual(page.path[0:4], subpage.path[0:4])

        subpage = publish_page(subpage, self.user_super, 'en')
        # tree id must stay the same
        self.assertEqual(page.path[0:4], subpage.path[0:4])

        # published pages must also have the same root-path
        self.assertEqual(page.publisher_public.path[0:4], subpage.publisher_public.path[0:4])

        #check attributes
        self.check_published_page_attributes(page)
        self.check_published_page_attributes(subpage)

    def test_super_add_page_to_root(self):
        """Create page which is not under moderation in root, and check if
        some properties are correct.
        """
        # create page under root
        page = create_page("page", "nav_playground.html", "en")

        # public must not exist
        self.assertFalse(page.publisher_public)

    def test_moderator_flags(self):
        """Add page under slave_home and check its flag
        """
        page = create_page("page", "nav_playground.html", "en",
                           parent=self.slave_page)

        # No public version
        self.assertIsNone(page.publisher_public)
        self.assertFalse(page.publisher_public_id)

        # check publish box
        page = publish_page(page, self.user_slave, 'en')

        # public page must not exist because of parent
        self.assertFalse(page.publisher_public)

        # waiting for parents
        self.assertEqual(page.get_publisher_state('en'), PUBLISHER_STATE_PENDING)

        # publish slave page
        self.slave_page = self.slave_page.reload()
        slave_page = publish_page(self.slave_page, self.user_master, 'en')

        self.assertFalse(page.publisher_public)
        self.assertTrue(slave_page.publisher_public)

    def test_plugins_get_published(self):
        # create page under root
        page = create_page("page", "nav_playground.html", "en")
        placeholder = page.placeholders.all()[0]
        add_plugin(placeholder, "TextPlugin", "en", body="test")
        # public must not exist
        self.assertEqual(CMSPlugin.objects.all().count(), 1)
        publish_page(page, self.user_super, 'en')
        self.assertEqual(CMSPlugin.objects.all().count(), 2)

    def test_remove_plugin_page_under_moderation(self):
        # login as slave and create page
        page = create_page("page", "nav_playground.html", "en", parent=self.slave_page)

        # add plugin
        placeholder = page.placeholders.all()[0]
        plugin = add_plugin(placeholder, "TextPlugin", "en", body="test")

        # publish page
        page = self.reload(page)
        page = publish_page(page, self.user_slave, 'en')

        # only the draft plugin should exist
        self.assertEqual(CMSPlugin.objects.all().count(), 1)

        # page should require approval
        self.assertEqual(page.get_publisher_state('en'), PUBLISHER_STATE_PENDING)

        # master approves and publishes the page
        # first approve slave-home
        slave_page = self.reload(self.slave_page)
        publish_page(slave_page, self.user_master, 'en')
        page = self.reload(page)
        page = publish_page(page, self.user_master, 'en')

        # draft and public plugins should now exist
        self.assertEqual(CMSPlugin.objects.all().count(), 2)

        # login as slave and delete the plugin - should require moderation
        with self.login_user_context(self.user_slave):
            plugin_data = {
                'plugin_id': plugin.pk
            }
            remove_url = URL_CMS_PLUGIN_REMOVE + "%s/" % plugin.pk
            response = self.client.post(remove_url, plugin_data)
            self.assertEqual(response.status_code, 302)

            # there should only be a public plugin - since the draft has been deleted
            self.assertEqual(CMSPlugin.objects.all().count(), 1)

            page = self.reload(page)

            # login as super user and approve/publish the page
            publish_page(page, self.user_super, 'en')

            # there should now be 0 plugins
            self.assertEqual(CMSPlugin.objects.all().count(), 0)

    def test_superuser_can_view(self):
        url = self.page_b.get_absolute_url(language='en')
        with self.login_user_context(self.user_super):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)

    def test_staff_can_view(self):
        url = self.page_b.get_absolute_url(language='en')
        all_view_perms = PagePermission.objects.filter(can_view=True)
        # verifiy that the user_staff has access to this page
        has_perm = False
        for perm in all_view_perms:
            if perm.page == self.page_b:
                if perm.user == self.user_staff:
                    has_perm = True
        self.assertEqual(has_perm, False)
        login_ok = self.client.login(username=getattr(self.user_staff, get_user_model().USERNAME_FIELD),
                                     password=getattr(self.user_staff, get_user_model().USERNAME_FIELD))
        self.assertTrue(login_ok)

        # really logged in
        self.assertTrue('_auth_user_id' in self.client.session)
        login_user_id = self.client.session.get('_auth_user_id')
        user = get_user_model().objects.get(pk=self.user_staff.pk)
        self.assertEqual(str(login_user_id), str(user.id))
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_user_normal_can_view(self):
        url = self.page_b.get_absolute_url(language='en')
        all_view_perms = PagePermission.objects.filter(can_view=True)
        # verifiy that the normal_user has access to this page
        normal_has_perm = False
        for perm in all_view_perms:
            if perm.page == self.page_b:
                if perm.user == self.user_normal:
                    normal_has_perm = True
        self.assertTrue(normal_has_perm)
        with self.login_user_context(self.user_normal):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)

        # verifiy that the user_non_global has not access to this page
        non_global_has_perm = False
        for perm in all_view_perms:
            if perm.page == self.page_b:
                if perm.user == self.user_non_global:
                    non_global_has_perm = True
        self.assertFalse(non_global_has_perm)
        with self.login_user_context(self.user_non_global):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404)

        # non logged in user
        response = self.client.get(url)
        self.assertEqual(response.status_code, 404)

    def test_user_globalpermission(self):
        # Global user
        user_global = self._create_user("global")

        with self.login_user_context(self.user_super):
            user_global = create_page_user(user_global, user_global)
            user_global.is_staff = False
            user_global.save() # Prevent is_staff permission
            global_page = create_page("global", "nav_playground.html", "en",
                                      published=True)
            # Removed call since global page user doesn't have publish permission
            #global_page = publish_page(global_page, user_global)
            # it's allowed for the normal user to view the page
            assign_user_to_page(global_page, user_global,
                                global_permission=True, can_view=True)

        url = global_page.get_absolute_url('en')
        all_view_perms = PagePermission.objects.filter(can_view=True)
        has_perm = False
        for perm in all_view_perms:
            if perm.page == self.page_b and perm.user == user_global:
                has_perm = True
        self.assertEqual(has_perm, False)

        global_page_perm_q = Q(user=user_global) & Q(can_view=True)
        global_view_perms = GlobalPagePermission.objects.filter(global_page_perm_q).exists()
        self.assertEqual(global_view_perms, True)

        # user_global
        with self.login_user_context(user_global):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 200)
            # self.non_user_global
        has_perm = False
        for perm in all_view_perms:
            if perm.page == self.page_b and perm.user == self.user_non_global:
                has_perm = True
        self.assertEqual(has_perm, False)

        global_page_perm_q = Q(user=self.user_non_global) & Q(can_view=True)
        global_view_perms = GlobalPagePermission.objects.filter(global_page_perm_q).exists()
        self.assertEqual(global_view_perms, False)

        with self.login_user_context(self.user_non_global):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404)

    def test_anonymous_user_public_for_all(self):
        url = self.page_b.get_absolute_url('en')
        with self.settings(CMS_PUBLIC_FOR='all'):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404)

    def test_anonymous_user_public_for_none(self):
        # default of when to show pages to anonymous user doesn't take
        # global permissions into account
        url = self.page_b.get_absolute_url('en')
        with self.settings(CMS_PUBLIC_FOR=None):
            response = self.client.get(url)
            self.assertEqual(response.status_code, 404)


@override_settings(CMS_PERMISSION=True)
class PatricksMoveTest(CMSTestCase):
    """
    Fixtures contains 3 users and 1 published page and some other stuff

    Users:
        1. `super`: superuser
        2. `master`: user with permissions to all applications
        3. `slave`: user assigned to page `slave-home`

    Pages:
        1. `home`:
            - published page
            - master can do anything on its subpages, but not on home!

        2. `master`:
            - published page
            - crated by super
            - `master` can do anything on it and its descendants
            - subpages:

        3.       `slave-home`:
                    - not published
                    - assigned slave user which can add/change/delete/
                      move/publish/moderate this page and its descendants
                    - `master` user want to moderate this page and all descendants

        4. `pageA`:
            - created by super
            - master can add/change/delete on it and descendants
    """

    def setUp(self):
        # create super user
        self.user_super = self._create_user("super", True, True)

        with self.login_user_context(self.user_super):
            self.home_page = create_page("home", "nav_playground.html", "en",
                                         created_by=self.user_super)

            # master page & master user

            self.master_page = create_page("master", "nav_playground.html", "en")

            # create master user
            self.user_master = self._create_user("master", True)
            self.user_master.user_permissions.add(Permission.objects.get(codename='publish_page'))
            #self.user_master = create_page_user(self.user_super, master, grant_all=True)

            # assign master user under home page
            assign_user_to_page(self.home_page, self.user_master,
                                grant_on=ACCESS_DESCENDANTS, grant_all=True)

            # and to master page
            assign_user_to_page(self.master_page, self.user_master, grant_all=True)

            # slave page & slave user

            self.slave_page = create_page("slave-home", "nav_playground.html", "en",
                                          parent=self.master_page, created_by=self.user_super)
            slave = self._create_user("slave", True)
            self.user_slave = create_page_user(self.user_super, slave, can_add_page=True,
                                               can_change_page=True, can_delete_page=True)

            assign_user_to_page(self.slave_page, self.user_slave, grant_all=True)

            # create page_a - sample page from master

            page_a = create_page("pageA", "nav_playground.html", "en",
                                 created_by=self.user_super)
            assign_user_to_page(page_a, self.user_master,
                                can_add=True, can_change=True, can_delete=True, can_publish=True,
                                can_move_page=True)

            # publish after creating all drafts
            publish_page(self.home_page, self.user_super, 'en')
            publish_page(self.master_page, self.user_super, 'en')

        with self.login_user_context(self.user_slave):
            # all of them are under moderation...
            self.pa = create_page("pa", "nav_playground.html", "en", parent=self.slave_page)
            self.pb = create_page("pb", "nav_playground.html", "en", parent=self.pa, position="right")
            self.pc = create_page("pc", "nav_playground.html", "en", parent=self.pb, position="right")

            self.pd = create_page("pd", "nav_playground.html", "en", parent=self.pb)
            self.pe = create_page("pe", "nav_playground.html", "en", parent=self.pd, position="right")

            self.pf = create_page("pf", "nav_playground.html", "en", parent=self.pe)
            self.pg = create_page("pg", "nav_playground.html", "en", parent=self.pf, position="right")
            self.ph = create_page("ph", "nav_playground.html", "en", parent=self.pf, position="right")

            self.assertFalse(self.pg.publisher_public)

            # login as master for approval
            self.slave_page = self.slave_page.reload()

            publish_page(self.slave_page, self.user_master, 'en')

            # publish and approve them all
            publish_page(self.pa, self.user_master, 'en')
            publish_page(self.pb, self.user_master, 'en')
            publish_page(self.pc, self.user_master, 'en')
            publish_page(self.pd, self.user_master, 'en')
            publish_page(self.pe, self.user_master, 'en')
            publish_page(self.pf, self.user_master, 'en')
            publish_page(self.pg, self.user_master, 'en')
            publish_page(self.ph, self.user_master, 'en')
            self.reload_pages()

    def reload_pages(self):
        self.pa = self.pa.reload()
        self.pb = self.pb.reload()
        self.pc = self.pc.reload()
        self.pd = self.pd.reload()
        self.pe = self.pe.reload()
        self.pf = self.pf.reload()
        self.pg = self.pg.reload()
        self.ph = self.ph.reload()


    def test_patricks_move(self):
        """

        Tests permmod when moving trees of pages.

        1. build following tree (master node is approved and published)

                 slave-home
                /    |    \
               A     B     C
                   /  \
                  D    E
                    /  |  \
                   F   G   H

        2. perform move operations:
            1. move G under C
            2. move E under G

                 slave-home
                /    |    \
               A     B     C
                   /        \
                  D          G
                              \
                               E
                             /   \
                            F     H

        3. approve nodes in following order:
            1. approve H
            2. approve G
            3. approve E
            4. approve F
        """
        # TODO: this takes 5 seconds to run on my MBP. That's TOO LONG!
        self.assertEqual(self.pg.parent_id, self.pe.pk)
        self.assertEqual(self.pg.publisher_public.parent_id, self.pe.publisher_public_id)
        # perform moves under slave...
        self.move_page(self.pg, self.pc)
        self.reload_pages()
        # Draft page is now under PC
        self.assertEqual(self.pg.parent_id, self.pc.pk)
        # Public page is under PC
        self.assertEqual(self.pg.publisher_public.parent_id, self.pc.publisher_public_id)
        self.assertEqual(self.pg.publisher_public.parent.get_absolute_url(),
                         self.pc.publisher_public.get_absolute_url())
        self.assertEqual(self.pg.get_absolute_url(), self.pg.publisher_public.get_absolute_url())
        self.move_page(self.pe, self.pg)
        self.reload_pages()
        self.assertEqual(self.pe.parent_id, self.pg.pk)
        self.assertEqual(self.pe.publisher_public.parent_id, self.pg.publisher_public_id)
        self.ph = self.ph.reload()
        # check urls - they should stay be the same now after the move
        self.assertEqual(
            self.pg.publisher_public.get_absolute_url(),
            self.pg.get_absolute_url()
        )
        self.assertEqual(
            self.ph.publisher_public.get_absolute_url(),
            self.ph.get_absolute_url()
        )

        # public parent check after move
        self.assertEqual(self.pg.publisher_public.parent.pk, self.pc.publisher_public_id)
        self.assertEqual(self.pe.publisher_public.parent.pk, self.pg.publisher_public_id)
        self.assertEqual(self.ph.publisher_public.parent.pk, self.pe.publisher_public_id)

        # check if urls are correct after move
        self.assertEqual(
            self.pg.publisher_public.get_absolute_url(),
            u'%smaster/slave-home/pc/pg/' % self.get_pages_root()
        )
        self.assertEqual(
            self.ph.publisher_public.get_absolute_url(),
            u'%smaster/slave-home/pc/pg/pe/ph/' % self.get_pages_root()
        )


class ModeratorSwitchCommandTest(CMSTestCase):
    def test_switch_moderator_on(self):
        with force_language("en"):
            pages_root = unquote(reverse("pages-root"))
        page1 = create_page('page', 'nav_playground.html', 'en', published=True)
        with disable_logger(log):
            call_command('cms', 'moderator', 'on')
        with force_language("en"):
            path = page1.get_absolute_url()[len(pages_root):].strip('/')
            page2 = get_page_from_path(path)
        self.assertEqual(page1.get_absolute_url(), page2.get_absolute_url())

    def test_table_name_patching(self):
        """
        This tests the plugin models patching when publishing from the command line
        """
        self.get_superuser()
        create_page("The page!", "nav_playground.html", "en", published=True)
        draft = Page.objects.drafts()[0]
        draft.reverse_id = 'a_test'  # we have to change *something*
        draft.save()
        add_plugin(draft.placeholders.get(slot=u"body"),
                   u"TextPlugin", u"en", body="Test content")
        draft.publish('en')
        add_plugin(draft.placeholders.get(slot=u"body"),
                   u"TextPlugin", u"en", body="Test content")

        # Manually undoing table name patching
        Text._meta.db_table = 'djangocms_text_ckeditor_text'
        plugin_pool.patched = False

        with disable_logger(log):
            call_command('cms', 'moderator', 'on')
        # Sanity check the database (we should have one draft and one public)
        not_drafts = len(Page.objects.filter(publisher_is_draft=False))
        drafts = len(Page.objects.filter(publisher_is_draft=True))
        self.assertEqual(not_drafts, 1)
        self.assertEqual(drafts, 1)

    def test_switch_moderator_off(self):
        with force_language("en"):
            pages_root = unquote(reverse("pages-root"))
            page1 = create_page('page', 'nav_playground.html', 'en', published=True)
            path = page1.get_absolute_url()[len(pages_root):].strip('/')
            page2 = get_page_from_path(path)
            self.assertIsNotNone(page2)
            self.assertEqual(page1.get_absolute_url(), page2.get_absolute_url())

    def tearDown(self):
        plugin_pool.patched = False
        plugin_pool.set_plugin_meta()


class ViewPermissionBaseTests(CMSTestCase):

    def setUp(self):
        self.page = create_page('testpage', 'nav_playground.html', 'en')

    def get_request(self, user=None):
        attrs = {
            'user': user or AnonymousUser(),
            'REQUEST': {},
            'POST': {},
            'GET': {},
            'session': {},
        }
        return type('Request', (object,), attrs)


@override_settings(
    CMS_PERMISSION=False,
    CMS_PUBLIC_FOR='staff',
)
class BasicViewPermissionTests(ViewPermissionBaseTests):
    """
    Test functionality with CMS_PERMISSION set to false, as this is the
    normal use case
    """

    @override_settings(CMS_PUBLIC_FOR="all")
    def test_unauth_public(self):
        request = self.get_request()
        with self.assertNumQueries(0):
            self.assertTrue(self.page.has_view_permission(request))

        self.assertEqual(get_visible_pages(request, [self.page], self.page.site),
                         [self.page.pk])

    def test_unauth_non_access(self):
        request = self.get_request()
        with self.assertNumQueries(0):
            self.assertFalse(self.page.has_view_permission(request))

        self.assertEqual(get_visible_pages(request, [self.page], self.page.site),
                         [])

    @override_settings(CMS_PUBLIC_FOR="all")
    def test_staff_public_all(self):
        request = self.get_request(self.get_staff_user_with_no_permissions())
        with self.assertNumQueries(0):
            self.assertTrue(self.page.has_view_permission(request))

        self.assertEqual(get_visible_pages(request, [self.page], self.page.site),
                         [self.page.pk])

    def test_staff_public_staff(self):
        request = self.get_request(self.get_staff_user_with_no_permissions())
        with self.assertNumQueries(0):
            self.assertTrue(self.page.has_view_permission(request))

        self.assertEqual(get_visible_pages(request, [self.page], self.page.site),
                         [self.page.pk])

    @override_settings(CMS_PUBLIC_FOR="none")
    def test_staff_basic_auth(self):
        request = self.get_request(self.get_staff_user_with_no_permissions())
        with self.assertNumQueries(0):
            self.assertTrue(self.page.has_view_permission(request))

        self.assertEqual(get_visible_pages(request, [self.page], self.page.site),
                         [self.page.pk])

    @override_settings(CMS_PUBLIC_FOR="none")
    def test_normal_basic_auth(self):
        request = self.get_request(self.get_standard_user())
        with self.assertNumQueries(0):
            self.assertTrue(self.page.has_view_permission(request))

        self.assertEqual(get_visible_pages(request, [self.page], self.page.site),
                         [self.page.pk])


@override_settings(
    CMS_PERMISSION=True,
    CMS_PUBLIC_FOR='none'
)
class UnrestrictedViewPermissionTests(ViewPermissionBaseTests):
    """
        Test functionality with CMS_PERMISSION set to True but no restrictions
        apply to this specific page
    """

    def test_unauth_non_access(self):
        request = self.get_request()
        with self.assertNumQueries(1):
            """
            The query is:
            PagePermission query for the affected page (is the page restricted?)
            """
            self.assertFalse(self.page.has_view_permission(request))
        with self.assertNumQueries(0):
            self.assertFalse(self.page.has_view_permission(request))  # test cache

        self.assertEqual(get_visible_pages(request, [self.page], self.page.site),
                         [])

    def test_global_access(self):
        user = self.get_standard_user()
        GlobalPagePermission.objects.create(can_view=True, user=user)
        request = self.get_request(user)
        with self.assertNumQueries(2):
            """The queries are:
            PagePermission query for the affected page (is the page restricted?)
            GlobalPagePermission query for the page site
            """
            self.assertTrue(self.page.has_view_permission(request))

        with self.assertNumQueries(0):
            self.assertTrue(self.page.has_view_permission(request))  # test cache

        self.assertEqual(get_visible_pages(request, [self.page], self.page.site),
                         [self.page.pk])

    def test_normal_denied(self):
        request = self.get_request(self.get_standard_user())
        with self.assertNumQueries(4):
            """
            The queries are:
            PagePermission query for the affected page (is the page restricted?)
            GlobalPagePermission query for the page site
            User permissions query
            Content type query
             """
            self.assertFalse(self.page.has_view_permission(request))

        with self.assertNumQueries(0):
            self.assertFalse(self.page.has_view_permission(request))  # test cache

        self.assertEqual(get_visible_pages(request, [self.page], self.page.site),
                         [])


@override_settings(
    CMS_PERMISSION=True,
    CMS_PUBLIC_FOR='all'
)
class RestrictedViewPermissionTests(ViewPermissionBaseTests):
    """
    Test functionality with CMS_PERMISSION set to True and view restrictions
    apply to this specific page
    """
    def setUp(self):
        super(RestrictedViewPermissionTests, self).setUp()
        self.group = Group.objects.create(name='testgroup')
        self.pages = [self.page]
        self.expected = [self.page.pk]
        PagePermission.objects.create(page=self.page, group=self.group, can_view=True, grant_on=ACCESS_PAGE)

    def test_unauthed(self):
        request = self.get_request()
        with self.assertNumQueries(1):
            """The queries are:
            PagePermission query for the affected page (is the page restricted?)
            """
            self.assertFalse(self.page.has_view_permission(request))

        with self.assertNumQueries(0):
            self.assertFalse(self.page.has_view_permission(request))  # test cache

        self.assertEqual(get_visible_pages(request, self.pages, self.page.site),
                         [])

    def test_page_permissions(self):
        user = self.get_standard_user()
        request = self.get_request(user)
        PagePermission.objects.create(can_view=True, user=user, page=self.page, grant_on=ACCESS_PAGE)
        with self.assertNumQueries(3):
            """
            The queries are:
            PagePermission query (is this page restricted)
            GlobalpagePermission query for user
            PagePermission query for this user
            """
            self.assertTrue(self.page.has_view_permission(request))

        with self.assertNumQueries(0):
            self.assertTrue(self.page.has_view_permission(request))  # test cache

        self.assertEqual(get_visible_pages(request, self.pages, self.page.site),
                         self.expected)

    def test_page_group_permissions(self):
        user = self.get_standard_user()
        user.groups.add(self.group)
        request = self.get_request(user)
        with self.assertNumQueries(3):
            self.assertTrue(self.page.has_view_permission(request))

        with self.assertNumQueries(0):
            self.assertTrue(self.page.has_view_permission(request))  # test cache

        self.assertEqual(get_visible_pages(request, self.pages, self.page.site),
                         self.expected)

    def test_global_permission(self):
        user = self.get_standard_user()
        GlobalPagePermission.objects.create(can_view=True, user=user)
        request = self.get_request(user)
        with self.assertNumQueries(2):
            """
            The queries are:
            PagePermission query (is this page restricted)
            GlobalpagePermission query for user
            """
            self.assertTrue(self.page.has_view_permission(request))

        with self.assertNumQueries(0):
            self.assertTrue(self.page.has_view_permission(request))  # test cache

        self.assertEqual(get_visible_pages(request, self.pages, self.page.site),
                         self.expected)

    def test_basic_perm_denied(self):
        request = self.get_request(self.get_staff_user_with_no_permissions())
        with self.assertNumQueries(5):
            """
            The queries are:
            PagePermission query (is this page restricted)
            GlobalpagePermission query for user
            PagePermission query for this user
            Generic django permission lookup
            content type lookup by permission lookup
            """
            self.assertFalse(self.page.has_view_permission(request))

        with self.assertNumQueries(0):
            self.assertFalse(self.page.has_view_permission(request))  # test cache

        self.assertEqual(get_visible_pages(request, self.pages, self.page.site),
                         [])

    def test_basic_perm(self):
        user = self.get_standard_user()
        user.user_permissions.add(Permission.objects.get(codename='view_page'))
        request = self.get_request(user)
        with self.assertNumQueries(5):
            """
            The queries are:
            PagePermission query (is this page restricted)
            GlobalpagePermission query for user
            PagePermission query for this user
            Generic django permission lookup
            content type lookup by permission lookup
            """
            self.assertTrue(self.page.has_view_permission(request))

        with self.assertNumQueries(0):
            self.assertTrue(self.page.has_view_permission(request))  # test cache

        self.assertEqual(get_visible_pages(request, self.pages, self.page.site),
                         self.expected)


class PublicViewPermissionTests(RestrictedViewPermissionTests):
    """ Run the same tests as before, but on the public page instead. """

    def setUp(self):
        super(PublicViewPermissionTests, self).setUp()
        self.page.publish('en')
        self.pages = [self.page.publisher_public]
        self.expected = [self.page.publisher_public_id]


class GlobalPermissionTests(CMSTestCase):

    def test_sanity_check(self):
        """ Because we have a new manager, we'll do some basic checks."""
        # manager is still named the same.
        self.assertTrue(hasattr(GlobalPagePermission, 'objects'))
        self.assertEqual(0, GlobalPagePermission.objects.all().count())

        # we are correctly inheriting from BasicPagePermissionManager
        self.assertTrue(hasattr(GlobalPagePermission.objects, 'with_user'))

        # If we're using the new manager, we have extra methods which ensure
        # This site access OR all site access.
        self.assertTrue(hasattr(GlobalPagePermission.objects, 'user_has_permission'))
        # these are just convienence methods for the above.
        self.assertTrue(hasattr(GlobalPagePermission.objects, 'user_has_add_permission'))
        self.assertTrue(hasattr(GlobalPagePermission.objects, 'user_has_change_permission'))
        self.assertTrue(hasattr(GlobalPagePermission.objects, 'user_has_view_permission'))

    def test_emulate_admin_index(self):
        """ Call methods that emulate the adminsite instance's index.
        This test was basically the reason for the new manager, in light of the
        problem highlighted in ticket #1120, which asserts that giving a user
        no site-specific rights when creating a GlobalPagePermission should
        allow access to all sites.
        """
        # create and then ignore this user.
        superuser = self._create_user("super", is_staff=True, is_active=True,
                                      is_superuser=True)
        superuser.set_password("super")
        superuser.save()

        site_1 = Site.objects.get(pk=1)
        site_2 = Site.objects.create(domain='example2.com', name='example2.com')

        SITES = [site_1, site_2]

        # create 2 staff users
        USERS = [
            self._create_user("staff", is_staff=True, is_active=True),
            self._create_user("staff_2", is_staff=True, is_active=True),
        ]
        for user in USERS:
            user.set_password('staff')
            # re-use the same methods the UserPage form does.
            # Note that it internally calls .save(), as we've not done so.
            save_permissions({
                'can_add_page': True,
                'can_change_page': True,
                'can_delete_page': False
            }, user)

        GlobalPagePermission.objects.create(can_add=True, can_change=True,
                                            can_delete=False, user=USERS[0])
        # we're querying here to ensure that even though we've created two users
        # above, we should have successfully filtered to just one perm.
        self.assertEqual(1, GlobalPagePermission.objects.with_user(USERS[0]).count())

        # this will confirm explicit permissions still work, by adding the first
        # site instance to the many2many relationship 'sites'
        GlobalPagePermission.objects.create(can_add=True, can_change=True,
                                            can_delete=False,
                                            user=USERS[1]).sites.add(SITES[0])
        self.assertEqual(1, GlobalPagePermission.objects.with_user(USERS[1]).count())

        homepage = create_page(title="master", template="nav_playground.html",
                               language="en", in_navigation=True, slug='/')
        publish_page(page=homepage, user=superuser, language='en')

        with self.settings(CMS_PERMISSION=True):
            # for all users, they should have access to site 1
            request = RequestFactory().get(path='/', data={'site__exact': site_1.pk})
            # we need a session attribute for current_site(request), which is
            # used by has_page_add_permission_from_request and has_page_change_permission
            request.session = {}
            for user in USERS:
                # has_page_add_permission_from_request and has_page_change_permission both test
                # for this explicitly, to see if it's a superuser.
                request.user = user
                # Note, the query count is inflated by doing additional lookups
                # because there's a site param in the request.
                with self.assertNumQueries(FuzzyInt(6, 7)):
                    # PageAdmin swaps out the methods called for permissions
                    # if the setting is true, it makes use of cms.utils.permissions
                    self.assertTrue(has_page_add_permission_from_request(request))
                    self.assertTrue(has_page_change_permission(request))
                    # internally this calls PageAdmin.has_[add|change|delete]_permission()
                    self.assertEqual({'add': True, 'change': True, 'delete': False},
                                     site._registry[Page].get_model_perms(request))

            # can't use the above loop for this test, as we're testing that
            # user 1 has access, but user 2 does not, as they are only assigned
            # to site 1
            request = RequestFactory().get('/', data={'site__exact': site_2.pk})
            request.session = {}
            # As before, the query count is inflated by doing additional lookups
            # because there's a site param in the request
            with self.assertNumQueries(FuzzyInt(11, 20)):
                # this user shouldn't have access to site 2
                request.user = USERS[1]
                self.assertTrue(not has_page_add_permission_from_request(request))
                self.assertTrue(not has_page_change_permission(request))
                self.assertEqual({'add': False, 'change': False, 'delete': False},
                                 site._registry[Page].get_model_perms(request))
                # but, going back to the first user, they should.
                request = RequestFactory().get('/', data={'site__exact': site_2.pk})
                request.user = USERS[0]
                self.assertTrue(has_page_add_permission_from_request(request))
                self.assertTrue(has_page_change_permission(request))
                self.assertEqual({'add': True, 'change': True, 'delete': False},
                                 site._registry[Page].get_model_perms(request))

    def test_has_page_add_permission_with_target(self):
        page = create_page('Test', 'nav_playground.html', 'en')
        user = self._create_user('user')
        request = RequestFactory().get('/', data={'target': page.pk})
        request.session = {}
        request.user = user
        has_perm = has_page_add_permission_from_request(request)
        self.assertFalse(has_perm)
